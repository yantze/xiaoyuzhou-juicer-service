# 验证记录

日期：2026-09-21 UTC。

## 真实平台接口

- 官方 accounts 登录页可见手机号登录与扫码登录两种方式。
- 从当前官方公开前端核实 QR create/login 路由、请求字段与状态值。
- 使用本服务 Provider 创建二维码：HTTP 200，返回 id、url。
- 使用本服务 Provider 轮询一次：状态 WAITTING；尚未授权、未获得 Token。
- 读取 `6aa127229d3264778166855e` 单集公开元信息成功：
  标题 `Vol.1 AI最前沿的人已经不聊大模型了`，时长 5578 秒，存在文稿 media ID。
- 测试未输出或打包任何真实二维码 ID、二维码 URL 或账号凭证。

## 本地验证

- 7 项 pytest 测试通过。
- JavaScript 语法检查通过，Python 编译检查通过。
- Uvicorn 启动成功，`/health`、首页、会话端点通过 HTTP 检查。
- 本地地址未能由云浏览器访问；部署后的公网首页已通过浏览器检查。

## Railway 部署与线上验收

- 用户已确认创建私有仓库 `yantze/xiaoyuzhou-juicer-service`，代码已推送 main。
- 公网地址：https://transcript-reader-production.up.railway.app
- Railway 项目：`52dcc7bb-f2da-43c2-9d05-1afb98b58280`。
- production 环境：`a3d602a1-76f1-4588-8864-cb08506ddc2f`。
- transcript-reader 服务：`8716cf68-f182-4198-93c9-963e23f53bbf`。
- 部署 `4ab6f8f0-0e39-42eb-98c5-1a4911c1b87d` 状态 SUCCESS。
- 已挂载 `/data` 持久卷，配置独立加密密钥、Secure Cookie、一个副本。
- 公网首页、健康检查、会话接口均 HTTP 200。
- 线上二维码创建 HTTP 200 / WAITTING，二维码图片 HTTP 200，轮询 HTTP 200 / WAITTING。
- 未登录测试会话不能读取文稿；已清除该测试会话。
- 浏览器页面正常进入未连接状态，生成二维码按钮可用，获取文稿按钮按预期禁用。

仍需用户本人扫码确认，以验证真实 Token 与逐字稿接口的兼容性。
未经真实扫码，不宣称“扫码登录到逐字稿下载”端到端链路已验证。

## 测试替身范围

测试替身仅用于 tests/ 中，生产 Provider 始终连接平台真实接口。
模拟扫码确认不证明真实授权成功；模拟文稿也不会进入生产页面。

## Vercel / Supabase 适配验证（2026-10-03 UTC）

本次使用新数据库表，不迁移 Railway 登录状态或历史文稿。

- 新增 Vercel 原生 FastAPI `asgi.py` 入口和 `vercel.json`，配置函数最长 120 秒。
- `VERCEL=1` 时验证入口选择 PostgreSQL，不创建 SQLite 文件；缺少连接或加密密钥时明确报错。
- 新的 `xyz_juicer` schema 包含加密会话、跨实例租约和共享限流；初始化脚本可重复执行，
  验证重复初始化未改变已有的其他 schema 测试表。
- 14 项 pytest 通过，包括原有 7 项功能测试和 7 项新配置/PostgreSQL 测试。
- 当前容器不能创建系统用户或切换 UID，原生 PostgreSQL 的本地启动受限。
  数据库集成测试实际使用 PostgreSQL WASM 引擎 PGlite 0.5.8，经官方 `pglite-socket` 0.2.11
  TCP 适配器连接 Psycopg，未模拟 SQL 返回值；它不能代替 Supabase/原生 PostgreSQL 的全部环境验收。
- GitHub Actions 的 PostgreSQL 17 service 验证通过：
  https://github.com/yantze/xiaoyuzhou-juicer-service/actions/runs/37105374836 。
- 覆盖不同应用实例读取同一加密会话、原始 Token 不以明文存储、匿名/登录浏览器角色无数据读取权限、
  共享限流、过期租约拒绝旧请求写入、注销后拒绝恢复会话。
- 跨实例执行模拟扫码确认、Token 续期、下载失败后的新 Token 保留、Markdown/TXT/JSON 下载与注销；
  并发获取不会把同一 Token 续期两次。
- 在一次性数据库上实际执行 `scripts/migrate.py` 成功，首页、静态文件、`/health` 和新会话均可用。
- 使用生产 Provider 实测官方二维码创建：HTTP 200 / WAITTING；图片 HTTP 200 / image/png；
  轮询 HTTP 200 / WAITTING；测试结束后删除测试会话，未输出或保存真实二维码标识与账号 Token。
- 当前公开单集 `6aa127229d3264778166855e` 元信息读取成功，仍包含平台文稿 media ID。
- Python 编译、JavaScript 语法、Git diff 空白检查和依赖清单一致性检查通过。

### Vercel 生产发布与真实 Supabase 验收

- 用户完成 Vercel CLI 授权；创建独立项目 `xiaoyuzhou-juicer`，不覆盖其他项目。
- 关联已有 Supabase `supabase-violet-flower`，使用 Transaction pooler、SSL 与全新敏感加密密钥。
- Marketplace URL 带 `supa` 标记；新增兼容处理并验证不会丢失 SSL 设置或转义密码。
  最后本地回归 9 项通过；6 项数据库集成测试未在当前容器重复运行。
- 在 Vercel 临时部署中初始化真实 Supabase PostgreSQL 17.11；验证应用表可用、初始化时会话数为 0。
  初始化请求同时使用 Vercel 访问保护和独立请求头校验；临时部署已删除，正式站没有初始化路由。
- 正式地址：https://xiaoyuzhou-juicer.vercel.app 。
- 生产部署：`dpl_ADgB4F9Ecf6JwqdyF3TPwDv3VXkh`，状态 READY。
- 项目生产分支为 `feat/vercel-supabase`，函数实际运行于新加坡 `sin1`。
- 无需 Vercel 登录即可访问：首页、静态 JavaScript、`/health`、`/api/session` 均 HTTP 200。
- `/health` 实际检查 Supabase 三张应用表；新会话未连接账号，连续请求保持同一会话。
- Cookie 验证 Secure、HttpOnly、SameSite=Strict。
- 真实小宇宙接口：二维码创建 HTTP 200 / WAITTING，二维码图片 HTTP 200 / image/png，
  轮询 HTTP 200 / WAITTING；未登录获取逐字稿 HTTP 401。
- 扫码测试会话通过注销接口清除，HTTP 200；未迁移旧登录状态或历史文稿。

真实扫码确认与账号文稿获取仍需用户完成，不能把替身测试作为端到端生产验收。
