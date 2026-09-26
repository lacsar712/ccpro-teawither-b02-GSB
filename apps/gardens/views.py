from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DeleteView,
    ListView,
    UpdateView,
)

from .forms import GardenForm, GardenMergeForm, TroughForm, WitherBatchForm
from .models import Garden, GardenMergeError, Trough, WitherBatch


def _wants_htmx(request):
    return request.headers.get("HX-Request") == "true"


@login_required
def home(request):
    # 分园槽数：合并后源园自然消失、目标园计入迁入槽，
    # 与槽位列表按园过滤的行数同源（均取 Trough.garden_id）。
    garden_stats = list(
        Garden.objects.annotate(trough_total=Count("troughs")).order_by("name")
    )
    context = {
        "garden_count": Garden.objects.count(),
        "trough_count": Trough.objects.count(),
        "batch_count": WitherBatch.objects.count(),
        "ready_count": Trough.objects.filter(status=Trough.STATUS_READY).count(),
        "withering_count": Trough.objects.filter(
            status=Trough.STATUS_WITHERING
        ).count(),
        "loading_count": Trough.objects.filter(
            status=Trough.STATUS_LOADING
        ).count(),
        "garden_stats": garden_stats,
    }
    return render(request, "home.html", context)


# ---- Garden ----


class GardenListView(LoginRequiredMixin, ListView):
    model = Garden
    template_name = "gardens/list.html"
    context_object_name = "gardens"

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "gardens/_table.html",
                {"gardens": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class GardenCreateView(LoginRequiredMixin, CreateView):
    model = Garden
    form_class = GardenForm
    template_name = "gardens/form.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已创建")
        response = super().form_valid(form)
        if _wants_htmx(self.request):
            return redirect("garden_list")
        return response


class GardenUpdateView(LoginRequiredMixin, UpdateView):
    model = Garden
    form_class = GardenForm
    template_name = "gardens/form.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已更新")
        return super().form_valid(form)


class GardenDeleteView(LoginRequiredMixin, DeleteView):
    model = Garden
    template_name = "gardens/confirm_delete.html"
    success_url = reverse_lazy("garden_list")

    def form_valid(self, form):
        messages.success(self.request, "茶园已删除")
        return super().form_valid(form)


@login_required
def garden_merge(request, pk):
    """主管把源茶园整园并入目标茶园。

    GET 展示规则说明与目标园选择；POST 执行合并。
    萎凋工（非主管）一律拒绝：返回 403。
    """
    # 主管 = 后台员工账号（种子 admin 为超级用户；witherer 为普通萎凋工）
    if not request.user.is_staff:
        raise PermissionDenied("仅主管可发起茶园合并。")

    source = get_object_or_404(Garden, pk=pk)

    if request.method == "POST":
        form = GardenMergeForm(request.POST, source=source)
        if form.is_valid():
            target = form.cleaned_data["target"]
            try:
                moved, renames = source.merge_into(target)
            except GardenMergeError as exc:
                # 业务规则拒绝（萎凋中槽位等）：留在说明页并展示原因
                form.add_error(None, str(exc))
            else:
                detail = f"已将 {moved} 个槽位移交至「{target.name}」，源茶园「{source.name}」已删除"
                if renames:
                    renamed = "、".join(f"{old}→{new}" for old, new in renames)
                    detail += f"；同号自动重编号：{renamed}"
                messages.success(request, detail + "。")
                return redirect("garden_list")
    else:
        form = GardenMergeForm(source=source)

    source_troughs = source.troughs.all()
    withering = source_troughs.filter(status=Trough.STATUS_WITHERING).count()
    context = {
        "form": form,
        "source": source,
        "source_troughs": source_troughs,
        "source_withering": withering,
        # 仅列出可作目标的其他茶园，附各自萎凋中槽数供预判
        "targets": [
            (g, g.troughs.filter(status=Trough.STATUS_WITHERING).count())
            for g in Garden.objects.exclude(pk=source.pk)
        ],
    }
    return render(request, "gardens/merge.html", context)


# ---- Trough ----


class TroughListView(LoginRequiredMixin, ListView):
    model = Trough
    template_name = "troughs/list.html"
    context_object_name = "troughs"

    def get_queryset(self):
        qs = Trough.objects.select_related("garden").all()
        # 按园筛选：合并后按源园过滤行数为 0（源园已删除），
        # 目标园行数含迁入槽，与首页分园槽数一致。
        self.garden_id = self.request.GET.get("garden")
        self.selected_garden = None
        if self.garden_id:
            try:
                self.garden_id = int(self.garden_id)
            except (TypeError, ValueError):
                self.garden_id = None
            else:
                self.selected_garden = Garden.objects.filter(pk=self.garden_id).first()
                if self.selected_garden is None:
                    # 已删除的源园：过滤结果为空，而不是报错
                    qs = qs.none()
                else:
                    qs = qs.filter(garden_id=self.garden_id)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["gardens"] = Garden.objects.all()
        context["selected_garden"] = self.selected_garden
        context["selected_garden_id"] = self.garden_id
        return context

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "troughs/_table.html",
                {"troughs": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class TroughCreateView(LoginRequiredMixin, CreateView):
    model = Trough
    form_class = TroughForm
    template_name = "troughs/form.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已创建")
        return super().form_valid(form)


class TroughUpdateView(LoginRequiredMixin, UpdateView):
    model = Trough
    form_class = TroughForm
    template_name = "troughs/form.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已更新")
        return super().form_valid(form)


class TroughDeleteView(LoginRequiredMixin, DeleteView):
    model = Trough
    template_name = "troughs/confirm_delete.html"
    success_url = reverse_lazy("trough_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋槽已删除")
        return super().form_valid(form)


# ---- WitherBatch ----


class BatchListView(LoginRequiredMixin, ListView):
    model = WitherBatch
    template_name = "batches/list.html"
    context_object_name = "batches"

    def get_queryset(self):
        return WitherBatch.objects.select_related("trough", "trough__garden").all()

    def get(self, request, *args, **kwargs):
        self.object_list = self.get_queryset()
        if _wants_htmx(request):
            html = render_to_string(
                "batches/_table.html",
                {"batches": self.object_list},
                request=request,
            )
            return HttpResponse(html)
        return super().get(request, *args, **kwargs)


class BatchCreateView(LoginRequiredMixin, CreateView):
    model = WitherBatch
    form_class = WitherBatchForm
    template_name = "batches/form.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋批次已创建")
        return super().form_valid(form)


class BatchUpdateView(LoginRequiredMixin, UpdateView):
    model = WitherBatch
    form_class = WitherBatchForm
    template_name = "batches/form.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋批次已更新")
        return super().form_valid(form)


class BatchDeleteView(LoginRequiredMixin, DeleteView):
    model = WitherBatch
    template_name = "batches/confirm_delete.html"
    success_url = reverse_lazy("batch_list")

    def form_valid(self, form):
        messages.success(self.request, "萎凋批次已删除")
        return super().form_valid(form)
