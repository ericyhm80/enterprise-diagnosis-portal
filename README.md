# Enterprise Diagnosis Portal v1.4 — Render Free Pilot

面向 3–5 人的小规模免费 Pilot。

## 关键变化
- Web Service：Render Free
- Database：Render Free PostgreSQL
- 文字回答、材料勾选、录音、附件都保存在 PostgreSQL，而不是临时磁盘
- 单文件默认限制 12MB
- 不调用任何外部 AI
- 管理员后台可查看回答并下载录音/附件
- 数据库免费实例有到期时间，仅用于 Pilot，不作为正式生产环境

## 入口
- 客户：`/`
- 管理员：`/admin`
- 健康检查：`/health`

## 环境变量
- `DATABASE_URL`：Render Postgres 连接串
- `DIAG_ADMIN_TOKEN`：管理员口令
- `DIAG_MAX_UPLOAD_MB=12`

## 说明
正式生产版应迁移到持久化对象存储/数据库、企业本地基础设施，并补 MFA/RBAC/备份/审计策略。
