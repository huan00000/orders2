# orders2 部署

将本目录内容作为 GitHub 仓库根目录上传，保留 `.github/workflows/build.yml`。
默认分支 push 或在默认分支手动运行 Actions 后，测试通过即发布：
`ghcr.io/<小写 owner>/<小写 repository>:latest` 和 `sha-<完整提交 SHA>`。
其他分支和 PR 仅验证构建。支持 linux/amd64、linux/arm64、linux/arm/v7。
使用自动提供的 GITHUB_TOKEN（contents: read、packages: write），无需把交易密钥放入 GitHub。
如果上传到大仓库子目录，需要把 workflow 放到仓库根目录的 .github/workflows 并调整构建上下文及测试目录。

## Armbian 首次部署

需要 Docker Engine 和 Docker Compose v2。用 `uname -m` 确认架构：aarch64 对应 ARM64，armv7l 对应 ARMv7。
将 run.yml、.env.example、orderlist.example.js 放在服务器同一个部署目录，然后执行：

```sh
cp .env.example .env
chmod 600 .env
nano .env
mkdir -p data
```

填写 GHCR_IMAGE（不带 :latest）、API_KEY、API_SECRET。不要上传 .env。
已有机器人：先停止旧实例，再把它的 **orderlist.js** 复制为 `data/orderlist.js`，
可把旧 logs.md 复制为 `data/logs.md`。不要同时运行两个实例处理同一账户。
只有全新实例才执行以下初始化命令（不会覆盖已有文件）：

```sh
cp -n orderlist.example.js data/orderlist.js
```

若 GHCR 包为私有，先交互式登录：

```sh
docker login ghcr.io -u YOUR_GITHUB_USERNAME
```

密码输入具有 read:packages 权限且能访问该包的 classic PAT。
公共镜像无需登录。若推送已有包遇到 403，在包设置中授予该仓库 Actions 写入权限。

启动会发送真实交易 API 请求：

```sh
docker compose -f run.yml up -d --pull always
docker compose -f run.yml logs --tail 100 -f
```

更新到最新镜像时再次执行：

```sh
docker compose -f run.yml up -d --pull always
```

每次执行 up 会拉取 latest，镜像变化时重建容器；后台容器不会自行检查 GHCR 更新。
重启服务器后由 restart: unless-stopped 恢复运行，但不会因此自动拉取新镜像。
订单、锁文件和 logs.md 保存在宿主机 data 目录，更新镜像不会覆盖它们。
必须挂载整个目录，因为订单保存和日志清理使用原子替换。
缺少订单文件时容器会拒绝启动，避免意外重置订单历史。

## 日志和本地运行

main.py 使用的 LoggedSession 在启动时清理 logs.md，之后在日志写入时每 60 秒检查一次。
按带时区的时间戳保留最近 28 天（恰好处于边界的记录保留），以完整日志条目为单位删除。
停机期间不清理，下一次启动立即清理。文件开头说明保留。
控制台 Docker 日志另外按 10 MB × 3 文件轮转，不适用 28 天规则。

本地安装 `pip install -r requirements.txt`，通过同目录 .env 或环境变量设置密钥；
环境变量优先。默认数据目录仍是脚本目录，设置 DATA_DIR 可更改。
运行 `python main.py`；`--once` 只执行一轮，`--interval 60` 设置轮询间隔。
测试使用虚拟密钥并模拟 HTTP：

```sh
API_KEY=test-key API_SECRET=test-secret python -m unittest discover -v
```

构建上下文使用 .dockerignore 白名单，镜像只复制运行代码、依赖清单和空订单模板。
.env、真实订单、日志不进入镜像；.gitignore 同时排除这些文件。

参考：[GitHub GHCR](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)、
[Docker 多平台构建](https://docs.docker.com/build/ci/github-actions/multi-platform/)、
[Compose 拉取镜像](https://docs.docker.com/reference/cli/docker/compose/pull/)。
