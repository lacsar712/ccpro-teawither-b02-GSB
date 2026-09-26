from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Garden, GardenMergeError, Trough, WitherBatch

User = get_user_model()


class GardenMergeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin", "a@x.local", "123456")
        self.witherer = User.objects.create_user("witherer", "w@x.local", "123456")
        self.src = Garden.objects.create(name="源园", altitudeBand="600m")
        self.dst = Garden.objects.create(name="目标园", altitudeBand="800m")

    def make_trough(self, garden, code, status=Trough.STATUS_LOADING):
        return Trough.objects.create(
            garden=garden,
            troughCode=code,
            cultivar="福鼎大白",
            loadKg=Decimal("100.00"),
            status=status,
        )

    # ---- 权限 ----

    def test_witherer_cannot_merge(self):
        """萎凋工发起合并：拒绝（403），数据不变。"""
        self.make_trough(self.src, "S-1")
        self.client.force_login(self.witherer)
        resp = self.client.post(
            reverse("garden_merge", args=[self.src.pk]),
            {"target": self.dst.pk},
        )
        self.assertEqual(resp.status_code, 403)
        self.assertTrue(Garden.objects.filter(pk=self.src.pk).exists())
        self.assertEqual(Trough.objects.filter(garden=self.src).count(), 1)

    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("garden_merge", args=[self.src.pk]))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp.url)

    def test_admin_can_open_merge_page(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse("garden_merge", args=[self.src.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "合并规则")
        # 规则说明须与实现一致：同号 -M1 重编号、萎凋中拒绝
        self.assertContains(resp, "-M1")
        self.assertContains(resp, "萎凋中")

    # ---- 萎凋中拒绝 ----

    def test_reject_when_source_has_withering(self):
        self.make_trough(self.src, "S-1", Trough.STATUS_WITHERING)
        self.make_trough(self.dst, "D-1", Trough.STATUS_LOADING)
        with self.assertRaises(GardenMergeError):
            self.src.merge_into(self.dst)
        self.assertTrue(Garden.objects.filter(pk=self.src.pk).exists())
        self.assertEqual(Trough.objects.filter(garden=self.src).count(), 1)

    def test_reject_when_target_has_withering(self):
        self.make_trough(self.src, "S-1", Trough.STATUS_LOADING)
        self.make_trough(self.dst, "D-1", Trough.STATUS_WITHERING)
        with self.assertRaises(GardenMergeError):
            self.src.merge_into(self.dst)
        # 任一槽都不得被迁走
        self.assertEqual(Trough.objects.filter(garden=self.src).count(), 1)
        self.assertEqual(Trough.objects.filter(garden=self.dst).count(), 1)

    def test_reject_is_transactional_via_view(self):
        self.make_trough(self.src, "S-1", Trough.STATUS_WITHERING)
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("garden_merge", args=[self.src.pk]),
            {"target": self.dst.pk},
        )
        self.assertEqual(resp.status_code, 200)  # 留在说明页显示错误
        self.assertContains(resp, "萎凋中")
        self.assertTrue(Garden.objects.filter(pk=self.src.pk).exists())

    # ---- 成功合并 / 同号重编号 ----

    def test_merge_moves_troughs_and_deletes_source(self):
        t1 = self.make_trough(self.src, "S-1", Trough.STATUS_LOADING)
        t2 = self.make_trough(self.src, "S-2", Trough.STATUS_LOADING)
        moved, renames = self.src.merge_into(self.dst)
        self.assertEqual((moved, renames), (2, []))
        self.assertFalse(Garden.objects.filter(pk=self.src.pk).exists())
        self.assertEqual(set(Trough.objects.filter(garden=self.dst).values_list("pk", flat=True)),
                         {t1.pk, t2.pk})

    def test_duplicate_code_renumbered_with_m1_chain(self):
        # 目标园已有 A-01，源园也有 A-01；源园另有 A-01-M1 触发链式递增
        self.make_trough(self.dst, "A-01")
        dup = self.make_trough(self.src, "A-01")
        chain = self.make_trough(self.src, "A-01-M1")
        moved, renames = self.src.merge_into(self.dst)
        self.assertEqual(moved, 2)
        dup.refresh_from_db()
        chain.refresh_from_db()
        self.assertEqual(dup.troughCode, "A-01-M1")
        self.assertEqual(chain.troughCode, "A-01-M1-M1")
        self.assertEqual(dict(renames), {"A-01": "A-01-M1", "A-01-M1": "A-01-M1-M1"})
        # 目标园内编号仍然唯一
        codes = list(
            Trough.objects.filter(garden=self.dst).values_list("troughCode", flat=True)
        )
        self.assertEqual(len(codes), len(set(codes)))

    def test_batches_keep_trough_pk_but_show_target_garden(self):
        t = self.make_trough(self.src, "S-1", Trough.STATUS_LOADING)
        batch = WitherBatch.objects.create(
            trough=t,
            startedAt=timezone.now() - timezone.timedelta(hours=5),
            targetMoisture=Decimal("38.00"),
            actualMoisture=Decimal("37.00"),
            rollGrade="一级",
        )
        self.src.merge_into(self.dst)
        batch.refresh_from_db()
        self.assertEqual(batch.trough_id, t.pk)  # 批次仍挂原槽主键
        self.assertEqual(batch.trough.garden_id, self.dst.pk)
        self.assertEqual(str(batch), f"{self.dst.name}-S-1 @ {batch.startedAt:%Y-%m-%d %H:%M}")

    def test_source_unreachable_after_merge(self):
        self.make_trough(self.src, "S-1")
        src_pk = self.src.pk
        self.src.merge_into(self.dst)
        self.client.force_login(self.admin)
        for url in [
            reverse("garden_edit", args=[src_pk]),
            reverse("garden_delete", args=[src_pk]),
            reverse("garden_merge", args=[src_pk]),
        ]:
            self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_view_success_redirects_and_messages(self):
        self.make_trough(self.dst, "A-01")
        self.make_trough(self.src, "A-01")
        self.client.force_login(self.admin)
        resp = self.client.post(
            reverse("garden_merge", args=[self.src.pk]),
            {"target": self.dst.pk},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertRedirects(resp, reverse("garden_list"))
        self.assertContains(resp, "A-01→A-01-M1")
        self.assertFalse(Garden.objects.filter(pk=self.src.pk).exists())

    # ---- 首页分园槽数 / 按园筛选 ----

    def test_home_per_garden_counts_match_filtered_list(self):
        self.make_trough(self.src, "S-1")
        self.make_trough(self.src, "S-2")
        self.make_trough(self.dst, "D-1")
        self.client.force_login(self.admin)

        resp = self.client.get(reverse("home"))
        stats = {g.pk: g.trough_total for g in resp.context["garden_stats"]}
        self.assertEqual(stats, {self.src.pk: 2, self.dst.pk: 1})

        # 合并前：列表按园过滤行数 == 首页分园槽数
        for garden_pk, total in stats.items():
            row = self.client.get(reverse("trough_list"), {"garden": garden_pk})
            self.assertEqual(len(row.context["troughs"]), total)

        src_pk = self.src.pk
        self.src.merge_into(self.dst)  # 源园 2 槽全部迁入

        resp = self.client.get(reverse("home"))
        stats_after = {g.pk: g.trough_total for g in resp.context["garden_stats"]}
        self.assertNotIn(src_pk, stats_after)       # 源园消失
        self.assertEqual(stats_after[self.dst.pk], 3)    # 目标园增加迁入数

        # 合并后误差为 0：目标园列表行数 == 分园计数；源园（已删除）行数为 0
        row_dst = self.client.get(reverse("trough_list"), {"garden": self.dst.pk})
        self.assertEqual(len(row_dst.context["troughs"]), stats_after[self.dst.pk])
        row_src = self.client.get(reverse("trough_list"), {"garden": src_pk})
        self.assertEqual(len(row_src.context["troughs"]), 0)
        self.assertIsNone(row_src.context["selected_garden"])


class SeedDataTests(TestCase):
    def test_seed_has_duplicate_code_across_gardens(self):
        from apps.gardens.seed import ensure_seed_data

        ensure_seed_data()
        # 两园各有 A-01（园内唯一仍成立）
        self.assertEqual(
            Trough.objects.filter(troughCode="A-01").values("garden").distinct().count(),
            2,
        )
        for g in Garden.objects.all():
            codes = list(
                Trough.objects.filter(garden=g).values_list("troughCode", flat=True)
            )
            self.assertEqual(len(codes), len(set(codes)))
