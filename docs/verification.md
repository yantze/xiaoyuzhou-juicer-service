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
