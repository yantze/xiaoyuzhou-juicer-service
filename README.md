# 听见文字 · 小宇宙逐字稿服务

手机友好的个人阅读工具：后台创建官方扫码会话，用户在小宇宙 App
确认登录，后台接收并加密保存账号 Token，再获取平台已生成的 ASR 文稿。

## 功能

- 扫码状态：等待扫码、等待确认、过期、连接成功、明确失败。
- 后台读取小宇宙原始文稿，不把 shownotes/摘要当成逐字稿。
- 时间戳、关键词搜索、复制全文、Markdown/TXT/JSON 下载。
- 不同浏览器会话隔离；HttpOnly Cookie、CSRF 检查、请求限流。
- 凭证与最后一份文稿加密保存至 SQLite；断开连接清除本服务的数据。
- Token 轮换后立即持久化，避免后续下载失败导致刷新凭证丢失。
- 不把账号 Token 暴露给页面，不将账号 Token 发送给文稿 CDN。

## 本地运行

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
COOKIE_SECURE=false DATA_DIR=./data .venv/bin/uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080 --workers 1 --no-access-log
```

打开 http://localhost:8080 。生产环境须保留 `COOKIE_SECURE=true` 并通过 HTTPS 访问。
本机密钥会首次生成到 `DATA_DIR/encryption.key`，权限为 0600。

## Railway 部署

1. 将本目录提交到用户确定的 GitHub 仓库。
2. 在 Railway 创建独立服务并连接该仓库，使用仓库内 Dockerfile 与 railway.toml。
3. 添加持久卷，挂载到 `/data`。SQLite 与本机回退密钥必须跨部署保留。
4. 设 `DATA_DIR=/data`、`COOKIE_SECURE=true`。
5. 推荐生成 `TOKEN_ENCRYPTION_KEY` 并作为 Railway 变量保存：

   ```sh
   python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```

   不要把密钥放入 Git 仓库。不要随意变更此变量；旧数据需要原密钥才能解密。
6. 保持 **一个副本、一个 Uvicorn worker**。这是个人服务，SQLite 和进程内互斥不支持横向扩容。
7. 生成 Railway 域名，目标端口为 `PORT`（默认 8080）；健康检查为 `/health`。
8. 打开手机网页，生成二维码，用小宇宙 App 扫码并确认，再粘贴单集链接。

Dockerfile 信任平台传入的代理头，应部署在 Railway 的 HTTP 代理之后；
自行部署时请把 `--forwarded-allow-ips` 改为实际可信代理地址。

## 已验证与待验证

详见 `docs/verification.md`。不得把测试替身的登录结果作为真实登录成功证据。
最终端到端验收需要用户本人扫码，并选择其有权访问且存在逐字稿的单集。

## 限制

- 使用平台私有接口，平台更新可能导致失效；不是官方 OAuth 集成。
- QR 使用官方公开前端当前采用的协议。`XYZ_CLIENT_ID` 默认 `xyz-web`；
  `XYZ_MIDWAY_APP_ID` 默认来自当前官方前端，可在平台协议调整后更新。
- 若扫码成功却未返回可用账号 Token，服务会报错，不会伪装为已连接。
- 无平台文稿时明确提示，本版本不包含音频下载或付费 ASR 兜底。
- 手机同屏扫码通常需要相册识别支持或第二块屏幕，无法保证 App 支持从相册识别。
- Cookie/会话有效期 30 天；清理浏览器 Cookie 后需要重新扫码。
- 服务只保存每个会话最后一份文稿，不提供跨设备历史同步。
- 不公开分享全文，不绕过付费或账号权限；人名、术语需对照音频核验。
- 断开连接清除的是本服务保存的凭证，不等同于在小宇宙平台撤销全部设备登录。

## 验证

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
node --check app/static/app.js
```

测试使用明确的 test-only 凭证，覆盖会话隔离、CSRF、Token 轮换持久化、
扫码过期、重启存储、Cookie 提取、CDN 凭证隔离和输入验证。
