# OAS Android Control Plane

服务端保存每台设备的 JSON 配置、下发单设备任务租约、接收任务结果和调度补丁，
并提供受保护的设备管理页面。它不执行 OAS 任务，也不接收正常运行时的截图、OCR
输入或普通 Agent 日志。

本目录提供 Docker Compose 部署：Caddy 对外提供 HTTPS/WSS，FastAPI 控制面仅在
Docker 内网监听。生产 Agent 只接受 `wss://` 服务地址，因此需要可从公网访问的
域名和 TLS 证书。

## 部署前提

- Linux 服务器已安装 Docker Engine 和 Docker Compose Plugin（使用 `docker compose`）。
- 已将域名 A/AAAA 记录指向该服务器公网 IP，例如 `oas.example.com`。
- 云安全组和本机防火墙已允许 TCP `80`、`443`；不要暴露 `22270`。
- 服务器的 `80`、`443` 端口未被其它 Web 服务占用。若已有反向代理，请由现有代理
  为本服务提供同等的 HTTPS/WSS 转发，而不要同时启动此 Compose 中的 Caddy。

## 首次部署

在项目根目录拉取或上传完整仓库后执行：

```bash
cd OnmyojiAutoScript_Android/server
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少替换以下四项：

```ini
OAS_PUBLIC_HOST=oas.example.com
OAS_CADDY_EMAIL=admin@example.com
OAS_AGENT_SHARED_SECRET=<高强度随机 Agent 共享密钥>
OAS_AGENT_ADMIN_PASSWORD=<不同的高强度管理密码>
```

可用下面的命令生成密码；两个值必须不同：

```bash
openssl rand -base64 36
```

`OAS_PUBLIC_HOST` 只能填写域名，不要带 `https://`、路径或端口。`.env` 含部署密钥，
已被 Git 忽略，不应提交、复制进镜像或发给设备以外的人员。

启动并检查服务：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

首次启动时 Caddy 会通过 Let's Encrypt 自动申请证书；域名解析、80/443 端口开放前，
证书签发不会成功。运行正常后，管理页面地址为：

```text
https://oas.example.com/agents/ui
```

浏览器 Basic Auth 的用户名固定是 `admin`，密码是 `.env` 中的
`OAS_AGENT_ADMIN_PASSWORD`。也可以验证管理 API：

```bash
curl --fail --user 'admin:<管理密码>' \
  'https://oas.example.com/agents/devices'
```

首次尚未创建设备时，预期响应为 `[]`。

## Compose 组成与持久化数据

```text
Internet
  └─ Caddy :80/:443 (HTTPS/WSS、自动证书续期)
       └─ oas-control-plane :22270 (仅 Docker 内网)
```

| 文件或 Volume | 用途 |
| --- | --- |
| `Dockerfile` | 构建非 root 运行的 FastAPI 控制面镜像。 |
| `docker-compose.yml` | 启动控制面和 Caddy，不向主机发布 `22270`。 |
| `Caddyfile` | TLS、HTTP 到 HTTPS 跳转和 WebSocket 反向代理。 |
| `oas_device_config` | 保存 `config/devices/*.json`，重建容器后仍保留。 |
| `oas_server_log` | 保存 API 日志和 Agent 上传的失败现场。 |
| `caddy_data` | 保存 Let's Encrypt 证书和账户信息。 |

不要运行 `docker compose down -v`，它会删除这些命名 Volume，导致设备配置、错误现场
和证书全部丢失。普通的 `docker compose down` 不会删除 Volume。

控制面保存了 WebSocket 连接和任务租约等内存状态，因此当前架构只能运行一个
`oas-control-plane` 容器副本；不要扩容为多个 replicas。

## 注册设备并接入 Agent

1. 打开 `/agents/ui`，以 `admin` 登录。
2. 以 [template.agent-device.json](config/template.agent-device.json) 为模板创建设备；
   修改 `device_id`、`display_name`、游戏包名和屏幕分辨率以匹配真实设备。模板中的
   `Restart` 任务必须保留。
3. 在对应 Android/redroid 的 `/data/oas-agent/agent.env` 配置：

   ```ini
   OAS_AGENT_DEVICE_ID=<与服务端设备 device_id 完全一致>
   OAS_AGENT_SERVER_URL=wss://oas.example.com
   OAS_AGENT_SHARED_SECRET=<与服务端 .env 完全一致>
   OAS_AGENT_VERSION=2026.11.26
   OAS_AGENT_RESOURCE_VERSION=2026.11.26
   ```

`OAS_AGENT_SERVER_URL` 必须是服务根地址。不要填写 `/agent` 或 `/agent/ws`，Agent 会
自动连接到 `/agent/ws`，并在失败时向同一域名以 HTTPS 上传错误包。

## 日常运维

查看状态和日志：

```bash
docker compose ps
docker compose logs -f oas-control-plane
docker compose logs -f caddy
```

更新代码和镜像：

```bash
git pull
docker compose up -d --build
```

备份设备配置与错误日志到当前目录：

```bash
docker compose exec -T oas-control-plane \
  tar -C /app/config -czf - devices > oas-device-config-backup.tgz
docker compose exec -T oas-control-plane \
  tar -C /app -czf - log > oas-server-log-backup.tgz
```

服务启动时会清理七天前的服务日志和失败现场；需要长期保留时，请在启动前完成备份。
管理 API/UI 只受应用自身的 Basic Auth 保护，建议再由云防火墙或现有反向代理按固定
管理 IP 限制 `/agents/`，而 `/agent/` 必须允许设备访问。
