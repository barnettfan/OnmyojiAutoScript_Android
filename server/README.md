# OAS Android Control Plane

服务端保存每台设备的 JSON 配置、下发单设备任务租约、接收任务结果和调度补丁，
并提供受保护的设备管理页面。它不执行 OAS 任务，也不接收正常运行时的截图、OCR
输入或普通 Agent 日志。

本目录按“公网 IP + `ws://`”部署：Docker 直接发布 `22270`，Android/redroid Agent
直接连接控制面。这个模式没有 TLS：共享密钥、错误包上传请求头和管理后台密码都会
以明文传输。仅适合受信任网络，或已通过安全组/防火墙限制设备来源 IP 的场景。

## 部署前提

- Linux 服务器已安装 Docker Engine 和 Docker Compose Plugin（使用 `docker compose`）。
- 服务器公网 IP 是 `154.206.14.32`，云安全组和本机防火墙允许 TCP `22270`。
- 不要让任意公网来源访问此端口；请尽量只允许你的 Android/redroid 设备出口 IP 和
  管理电脑出口 IP。管理页面同样走该端口，Basic Auth 不具备传输加密。

## 首次部署

在项目根目录拉取或上传完整仓库后执行：

```bash
cd OnmyojiAutoScript_Android/server
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，替换密钥并确认端口：

```ini
OAS_CONTROL_PLANE_PORT=22270
OAS_AGENT_SHARED_SECRET=<高强度随机 Agent 共享密钥>
OAS_AGENT_ADMIN_PASSWORD=<不同的高强度管理密码>
```

可用下面的命令生成密码；两个值必须不同：

```bash
openssl rand -base64 36
```

`.env` 含部署密钥，已被 Git 忽略，不应提交或发送给无关人员。

启动并检查服务：

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f --tail=100
```

运行正常后，管理页面地址为：

```text
http://154.206.14.32:22270/agents/ui
```

浏览器 Basic Auth 的用户名固定是 `admin`，密码是 `.env` 中的
`OAS_AGENT_ADMIN_PASSWORD`。也可以验证管理 API：

```bash
curl --fail --user 'admin:<管理密码>' \
  'http://154.206.14.32:22270/agents/devices'
```

首次尚未创建设备时，预期响应为 `[]`。

## Compose 组成与持久化数据

```text
Android/redroid Agent ── ws://154.206.14.32:22270 ── oas-control-plane
管理浏览器          ── http://154.206.14.32:22270 ──┘
```

| 文件或 Volume | 用途 |
| --- | --- |
| `Dockerfile` | 构建非 root 运行的 FastAPI 控制面镜像。 |
| `docker-compose.yml` | 启动控制面，并直接发布 `${OAS_CONTROL_PLANE_PORT:-22270}`。 |
| `oas_device_config` | 保存 `config/devices/*.json`，重建容器后仍保留。 |
| `oas_server_log` | 保存 API 日志和 Agent 上传的失败现场。 |

不要运行 `docker compose down -v`，它会删除命名 Volume，导致设备配置和错误现场丢失。
普通的 `docker compose down` 不会删除 Volume。

控制面保存了 WebSocket 连接和任务租约等内存状态，因此当前架构只能运行一个
`oas-control-plane` 容器副本；不要扩容为多个 replicas。

## 注册设备并接入 Agent

1. 打开 `http://154.206.14.32:22270/agents/ui`，以 `admin` 登录。
2. 以 [template.agent-device.json](config/template.agent-device.json) 为模板创建设备；
   修改 `device_id`、`display_name`、游戏包名和屏幕分辨率以匹配真实设备。模板中的
   `Restart` 任务必须保留。
3. 在对应 Android/redroid 的 `/data/oas-agent/agent.env` 配置：

   ```ini
   OAS_AGENT_DEVICE_ID=<与服务端设备 device_id 完全一致>
   OAS_AGENT_SERVER_URL=ws://154.206.14.32:22270
   OAS_AGENT_ALLOW_INSECURE_WS=true
   OAS_AGENT_SHARED_SECRET=<与服务端 .env 完全一致>
   OAS_AGENT_VERSION=2026.11.26
   OAS_AGENT_RESOURCE_VERSION=2026.11.26
   ```

`OAS_AGENT_ALLOW_INSECURE_WS=true` 是直连模式的必填显式确认。不要填写 `/agent` 或
`/agent/ws`；Agent 会自动连接到 `/agent/ws`，并在失败时向同一 IP 以 HTTP 上传错误包。

## 日常运维

查看状态和日志：

```bash
docker compose ps
docker compose logs -f oas-control-plane
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
