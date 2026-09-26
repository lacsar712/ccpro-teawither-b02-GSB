from collections import namedtuple

from django.core.exceptions import ValidationError
from django.db import models, transaction


class Garden(models.Model):
    name = models.CharField("茶园名称", max_length=120)
    altitudeBand = models.CharField("海拔带", max_length=60)
    notes = models.TextField("备注", blank=True, default="")

    class Meta:
        ordering = ["name"]
        verbose_name = "茶园"
        verbose_name_plural = "茶园"

    def __str__(self):
        return self.name


class Trough(models.Model):
    STATUS_LOADING = "loading"
    STATUS_WITHERING = "withering"
    STATUS_READY = "ready"
    STATUS_CHOICES = [
        (STATUS_LOADING, "装叶中"),
        (STATUS_WITHERING, "萎凋中"),
        (STATUS_READY, "可下槽"),
    ]

    garden = models.ForeignKey(
        Garden,
        on_delete=models.CASCADE,
        related_name="troughs",
        verbose_name="茶园",
    )
    troughCode = models.CharField("槽位编号", max_length=40)
    cultivar = models.CharField("茶树品种", max_length=80)
    loadKg = models.DecimalField("装叶量(kg)", max_digits=10, decimal_places=2)
    status = models.CharField(
        "状态",
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_LOADING,
    )

    class Meta:
        ordering = ["garden__name", "troughCode"]
        verbose_name = "萎凋槽"
        verbose_name_plural = "萎凋槽"
        constraints = [
            models.UniqueConstraint(
                fields=["garden", "troughCode"],
                name="uniq_trough_code_per_garden",
            ),
        ]

    def __str__(self):
        return f"{self.garden.name}-{self.troughCode}"

    def latest_batch(self):
        return self.batches.order_by("-startedAt", "-id").first()

    def clean(self):
        super().clean()
        if self.status != self.STATUS_READY:
            return
        latest = None
        if self.pk:
            latest = (
                WitherBatch.objects.filter(trough_id=self.pk)
                .order_by("-startedAt", "-id")
                .first()
            )
        if latest is None or latest.actualMoisture is None or latest.actualMoisture > 40:
            raise ValidationError(
                {
                    "status": "无法设为可下槽：最新萎凋批次的实测含水率为空或高于 40%。"
                }
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class WitherBatch(models.Model):
    trough = models.ForeignKey(
        Trough,
        on_delete=models.CASCADE,
        related_name="batches",
        verbose_name="萎凋槽",
    )
    startedAt = models.DateTimeField("开始时间")
    targetMoisture = models.DecimalField(
        "目标含水率(%)", max_digits=5, decimal_places=2
    )
    actualMoisture = models.DecimalField(
        "实测含水率(%)",
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    rollGrade = models.CharField("揉捻等级", max_length=40)

    class Meta:
        ordering = ["-startedAt", "-id"]
        verbose_name = "萎凋批次"
        verbose_name_plural = "萎凋批次"

    def __str__(self):
        return f"{self.trough} @ {self.startedAt:%Y-%m-%d %H:%M}"


class MergeRejected(Exception):
    """整园合并被业务规则拒绝（携带可直接展示的中文原因）。"""


MergeResult = namedtuple("MergeResult", ["source_name", "moved", "renamed"])


def merge_gardens(source, target):
    """把源茶园 source 整园并入目标茶园 target。

    规则（与 README「整园合并」及合并页说明一致，前后一致）：
    1. 仅主管（超级用户）可发起（视图层校验）；源园或目标园存在
       「萎凋中」槽位时拒绝合并，须先处理槽位状态。
    2. 源园全部槽位迁入目标园，随后源园删除，其列表与详情不可再打开。
    3. 槽位编号在目标园已存在时自动重编号：在原编号后追加
       ``-M1``、``-M2``……取首个在目标园内不重名的编号。
    4. 萎凋批次仍挂原槽主键，展示园名随槽变为目标园。

    返回 MergeResult(source_name, moved, renamed)，renamed 为
    [(原编号, 新编号), ...]。拒绝时抛出 MergeRejected。
    """
    if source.pk == target.pk:
        raise MergeRejected("源茶园与目标茶园不能相同。")

    with transaction.atomic():
        source = Garden.objects.select_for_update().get(pk=source.pk)
        target = Garden.objects.select_for_update().get(pk=target.pk)

        src_withering = source.troughs.filter(
            status=Trough.STATUS_WITHERING
        ).count()
        tgt_withering = target.troughs.filter(
            status=Trough.STATUS_WITHERING
        ).count()
        if src_withering or tgt_withering:
            raise MergeRejected(
                "源园或目标园存在「萎凋中」槽位，禁止合并："
                f"「{source.name}」{src_withering} 个、"
                f"「{target.name}」{tgt_withering} 个。"
                "请先在槽位列表处理状态后再合并。"
            )

        existing = set(target.troughs.values_list("troughCode", flat=True))
        renamed = []
        moved = 0
        for trough in source.troughs.order_by("troughCode", "pk"):
            old_code = trough.troughCode
            new_code = old_code
            if new_code in existing:
                n = 1
                while f"{old_code}-M{n}" in existing:
                    n += 1
                new_code = f"{old_code}-M{n}"
                renamed.append((old_code, new_code))
            trough.garden = target
            trough.troughCode = new_code
            trough.save()
            existing.add(new_code)
            moved += 1

        source_name = source.name
        source.delete()

    return MergeResult(source_name=source_name, moved=moved, renamed=renamed)
