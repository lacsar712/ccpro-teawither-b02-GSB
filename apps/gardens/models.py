from django.core.exceptions import ValidationError
from django.db import models, transaction


class GardenMergeError(Exception):
    """茶园合并被业务规则拒绝（如存在萎凋中槽位）。"""


# 合并时同号重编号的后缀前缀：A-01 冲突 -> A-01-M1，再冲突 -> A-01-M2
MERGE_CODE_SUFFIX = "-M"


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

    def _rename_for_merge(self, code, taken):
        """返回目标园内未占用的迁入槽号。

        规则：编号不冲突则保留原号；冲突则在原编号后依次追加
        -M1、-M2、-M3……（MERGE_CODE_SUFFIX），直到园内唯一。
        ``taken`` 为已占用编号集合，会就地登记新编号。
        """
        if code not in taken:
            taken.add(code)
            return code
        seq = 1
        while True:
            candidate = f"{code}{MERGE_CODE_SUFFIX}{seq}"
            if candidate not in taken:
                taken.add(candidate)
                return candidate
            seq += 1

    def merge_into(self, target):
        """把本源园整园并入目标园：全部槽迁入 target，随后删除本源园。

        - 槽位（及其萎凋批次）主键不变，批次仍挂原槽，展示园名随之变为目标园；
        - 源园或目标园任一槽位处于「萎凋中」则拒绝（GardenMergeError），
          须先把状态处理为「装叶中/可下槽」；
        - 迁入槽号与目标园已有槽号重复时自动重编号，规则见 _rename_for_merge；
        - 整过程在单个事务内完成：先迁槽、后删源园，避免级联删除槽位。
        返回 (迁入槽数, 重编号列表[(旧号, 新号)])。
        """
        if target.pk == self.pk:
            raise GardenMergeError("源茶园与目标茶园不能相同。")

        with transaction.atomic():
            # 行锁，防止合并期间有槽位被改为萎凋中
            gardens = list(
                Garden.objects.select_for_update().filter(pk__in=[self.pk, target.pk])
            )
            if len(gardens) != 2:
                raise GardenMergeError("源茶园或目标茶园不存在，无法合并。")

            blocking = Trough.objects.filter(
                garden_id__in=[self.pk, target.pk],
                status=Trough.STATUS_WITHERING,
            )
            labels = {self.pk: "源", target.pk: "目标"}
            blockers = [
                f"{labels[t.garden_id]}茶园「{t.garden.name}」槽位 {t.troughCode}"
                for t in blocking.select_related("garden")
            ]
            if blockers:
                raise GardenMergeError(
                    "存在萎凋中槽位，已拒绝合并，请先处理状态："
                    + "、".join(blockers)
                    + "。"
                )

            taken = set(
                Trough.objects.filter(garden_id=target.pk).values_list(
                    "troughCode", flat=True
                )
            )
            moved = 0
            renames = []
            # 逐个迁移并重编号，依赖 (garden, troughCode) 唯一约束校验
            for trough in Trough.objects.filter(garden_id=self.pk).order_by("pk"):
                old_code = trough.troughCode
                new_code = self._rename_for_merge(old_code, taken)
                trough.garden_id = target.pk
                trough.troughCode = new_code
                trough.save(update_fields=["garden", "troughCode"])
                moved += 1
                if new_code != old_code:
                    renames.append((old_code, new_code))

            # 此时源园已无槽位，CASCADE 不会波及迁入的槽
            self.delete()

        return moved, renames


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
