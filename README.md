# TeaWither-01 · 茶萎凋台账

Django 5 + PostgreSQL 服务端渲染应用：Templates + HTMX + 自定义 CSS，无 Vue/React SPA。

## 技术栈

- Django 5、PostgreSQL
- Session 登录
- HTMX（CDN）局部刷新列表
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4100** |
| Postgres | **5440**（容器内 5432） |

数据库账号：`teawither` / `teawither` / 库名 `teawither`

## 快速启动

```bash
cd TeaWither/TeaWither-01
docker compose up --build -d
```

浏览器打开：http://localhost:4100

演示账号：

- `admin` / `123456`（超级用户）
- `witherer` / `123456`（普通用户）

容器启动时会自动：`migrate` → `seed_data` → `collectstatic` → `gunicorn`

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
# 确保本机 Postgres 监听 5440，或先 docker compose up -d db
set POSTGRES_HOST=localhost
set POSTGRES_PORT=5440
python manage.py migrate
python manage.py seed_data
python manage.py runserver 0.0.0.0:4100
```

## 业务模型

1. **Garden（茶园）**：`name`、`altitudeBand`、`notes`
2. **Trough（萎凋槽）**：归属茶园、`troughCode`、`cultivar`、`loadKg`、状态 `loading|withering|ready`；同一茶园内槽位编号唯一
3. **WitherBatch（萎凋批次）**：归属槽位、`startedAt`、`targetMoisture`、`actualMoisture`（可空）、`rollGrade`

**业务规则**：将槽位状态设为 `ready`（可下槽）时，若最新批次的 `actualMoisture` 为空或大于 40，抛出中文 `ValidationError`。

### 茶园合并（主管）

主管可在茶园列表点「合并」，把**源茶园整园并入目标茶园**：源园全部槽位迁入目标园，随后删除源园。规则（与合并页面说明一致）：

1. **权限**：仅主管（`is_staff`）可发起；萎凋工等普通用户发起一律拒绝（HTTP 403）。
2. **萎凋中拒绝**：源园或目标园任一槽位处于「萎凋中」即拒绝合并，错误信息列出具体槽位，须先把状态处理为「装叶中」或「可下槽」。
3. **同号自动重编号**（不拒绝合并）：迁入槽号在目标园已存在时，在原编号后依次追加 `-M1`、`-M2`、`-M3`……直到目标园内唯一（如 `A-01` → `A-01-M1`）；无冲突则保留原编号。重编号结果在成功提示中列出。
4. **数据保持**：槽位主键与萎凋批次主键均不变，批次仍挂原槽，经 `trough.garden` 展示的园名自动变为目标园；合并在单事务内完成（先迁槽、后删源园，避免级联删除槽位）。
5. **合并后**：源园列表与详情不可再打开（404），按源园筛选槽位行数为 0；首页「分园槽数」中源园消失、目标园增加迁入数，与槽位列表按园过滤行数误差为 0。

种子数据中两园含同号槽 `A-01`，且各有萎凋中槽位，可分别演示「萎凋拒绝」与「同号自动重编号」。

## 种子数据

```bash
python manage.py seed_data
```

幂等：已有茶园则只保证账号存在。亦可在环境变量 `TEAWITHER_AUTO_SEED=1` 时于 `post_migrate` 自动播种。

## 目录结构

```
TeaWither-01/
  manage.py
  requirements.txt
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  config/           # 项目配置
  apps/gardens/     # 模型、视图、种子命令
  templates/        # Django 模板
  static/css/       # 自定义样式（茶绿色顶栏）
```
