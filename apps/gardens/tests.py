from collections import Counter
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Garden,
    MergeRejected,
    Trough,
    WitherBatch,
    merge_gardens,
)
from .seed import ensure_seed_data


def make_trough(garden, code, status=Trough.STATUS_LOADING):
    return Trough.objects.create(
        garden=garden,
        troughCode=code,
        cultivar="福鼎大白",
        loadKg=Decimal("100.00"),
        status=status,
    )


class MergeGardensTests(TestCase):
    def setUp(self):
        self.source = Garden.objects.create(name="源园", altitudeBand="800m")
        self.target = Garden.objects.create(name="目标园", altitudeBand="600m")

    def test_moves_all_troughs_and_deletes_source(self):
        t1 = make_trough(self.source, "A-01")
        t2 = make_trough(self.source, "A-02")
        make_trough(self.target, "B-01")

        result = merge_gardens(self.source, self.target)

        self.assertEqual(result.source_name, "源园")
        self.assertEqual(result.moved, 2)
        self.assertEqual(result.renamed, [])
        self.assertFalse(Garden.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(Trough.objects.filter(garden=self.target).count(), 3)
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual(t1.garden_id, self.target.pk)
        self.assertEqual(t2.garden_id, self.target.pk)
        self.assertEqual(t1.troughCode, "A-01")

    def test_same_code_renumbered_with_suffix(self):
        make_trough(self.target, "A-01")
        make_trough(self.target, "A-01-M1")  # 首个后缀已被占用
        moved = make_trough(self.source, "A-01")

        result = merge_gardens(self.source, self.target)

        self.assertEqual(result.renamed, [("A-01", "A-01-M2")])
        moved.refresh_from_db()
        self.assertEqual(moved.troughCode, "A-01-M2")
        codes = list(
            Trough.objects.filter(garden=self.target).values_list(
                "troughCode", flat=True
            )
        )
        self.assertEqual(len(codes), len(set(codes)))  # 目标园内不重号

    def test_rejected_when_source_has_withering(self):
        make_trough(self.source, "A-01", status=Trough.STATUS_WITHERING)
        with self.assertRaises(MergeRejected):
            merge_gardens(self.source, self.target)
        # 拒绝后数据不变
        self.assertTrue(Garden.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(Trough.objects.filter(garden=self.source).count(), 1)

    def test_rejected_when_target_has_withering(self):
        make_trough(self.source, "A-01")
        make_trough(self.target, "B-01", status=Trough.STATUS_WITHERING)
        with self.assertRaises(MergeRejected):
            merge_gardens(self.source, self.target)
        self.assertTrue(Garden.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(Trough.objects.get(troughCode="A-01").garden_id, self.source.pk)

    def test_rejected_when_same_garden(self):
        with self.assertRaises(MergeRejected):
            merge_gardens(self.source, self.source)

    def test_batches_keep_trough_pk_and_show_target_garden(self):
        trough = make_trough(self.source, "A-01")
        batch = WitherBatch.objects.create(
            trough=trough,
            startedAt=timezone.now(),
            targetMoisture=Decimal("38.00"),
            actualMoisture=Decimal("37.00"),
            rollGrade="一级",
        )

        merge_gardens(self.source, self.target)

        batch.refresh_from_db()
        self.assertEqual(batch.trough_id, trough.pk)  # 批次仍挂原槽主键
        self.assertEqual(batch.trough.garden_id, self.target.pk)
        self.assertEqual(batch.trough.garden.name, "目标园")  # 展示园名为目标园


class MergeViewTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@x.com", "pw")
        self.witherer = User.objects.create_user("witherer", "w@x.com", "pw")
        self.source = Garden.objects.create(name="源园", altitudeBand="800m")
        self.target = Garden.objects.create(name="目标园", altitudeBand="600m")
        make_trough(self.source, "A-01")

    def test_superuser_can_merge_via_post(self):
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("garden_merge"),
            {"source": self.source.pk, "target": self.target.pk},
        )
        self.assertRedirects(resp, reverse("garden_list"))
        self.assertFalse(Garden.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(
            Trough.objects.get(troughCode="A-01").garden_id, self.target.pk
        )

    def test_witherer_merge_rejected(self):
        self.client.force_login(self.witherer)
        resp = self.client.post(
            reverse("garden_merge"),
            {"source": self.source.pk, "target": self.target.pk},
        )
        self.assertRedirects(resp, reverse("garden_list"))
        # 拒绝后源园与槽均未动
        self.assertTrue(Garden.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(
            Trough.objects.get(troughCode="A-01").garden_id, self.source.pk
        )
        # GET 合并页同样被拒
        resp = self.client.get(reverse("garden_merge"))
        self.assertRedirects(resp, reverse("garden_list"))

    def test_merge_page_shows_rules_for_superuser(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("garden_merge"))
        self.assertContains(resp, "萎凋中")
        self.assertContains(resp, "-M1")

    def test_withering_rejection_shown_on_form(self):
        make_trough(self.target, "B-01", status=Trough.STATUS_WITHERING)
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("garden_merge"),
            {"source": self.source.pk, "target": self.target.pk},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "萎凋中")
        self.assertTrue(Garden.objects.filter(pk=self.source.pk).exists())

    def test_source_gone_everywhere_after_merge(self):
        self.client.force_login(self.admin)
        self.client.post(
            reverse("garden_merge"),
            {"source": self.source.pk, "target": self.target.pk},
        )
        # 源园编辑/删除页不可再打开
        for name in ("garden_edit", "garden_delete"):
            resp = self.client.get(reverse(name, args=[self.source.pk]))
            self.assertEqual(resp.status_code, 404)
        # 茶园列表与槽过滤下拉均无源园
        resp = self.client.get(reverse("garden_list"))
        self.assertNotIn("源园", [g.name for g in resp.context["gardens"]])
        resp = self.client.get(reverse("trough_list"))
        self.assertNotIn("源园", [g.name for g in resp.context["gardens"]])
        # 按园筛槽：迁入槽只在目标园出现
        resp = self.client.get(
            reverse("trough_list"), {"garden": self.target.pk}
        )
        self.assertEqual(
            [t.troughCode for t in resp.context["troughs"]], ["A-01"]
        )
        resp = self.client.get(
            reverse("trough_list"), {"garden": self.source.pk}
        )
        self.assertEqual(len(resp.context["troughs"]), 0)


class HomeGardenStatsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "a@x.com", "pw")
        self.client.force_login(self.admin)

    def test_home_counts_match_filtered_list_after_merge(self):
        source = Garden.objects.create(name="源园", altitudeBand="800m")
        target = Garden.objects.create(name="目标园", altitudeBand="600m")
        make_trough(source, "A-01")
        make_trough(source, "A-02")
        make_trough(target, "A-01")

        self.client.post(
            reverse("garden_merge"),
            {"source": source.pk, "target": target.pk},
        )

        resp = self.client.get(reverse("home"))
        stats = {g.pk: g.trough_num for g in resp.context["garden_stats"]}
        # 源园消失，目标园增加迁入数（1 + 2 = 3）
        self.assertNotIn(source.pk, stats)
        self.assertEqual(stats[target.pk], 3)
        # 与槽列表按园过滤行数误差为 0
        for pk, num in stats.items():
            resp = self.client.get(reverse("trough_list"), {"garden": pk})
            self.assertEqual(len(resp.context["troughs"]), num)


class SeedDataTests(TestCase):
    def test_seed_has_same_code_risk_across_gardens(self):
        ensure_seed_data()
        codes = Counter(
            Trough.objects.values_list("troughCode", flat=True)
        )
        self.assertTrue(
            any(n > 1 for n in codes.values()),
            "种子数据应包含跨园同号槽位（同号风险）",
        )
