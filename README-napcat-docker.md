# NapCat x86_64 镜像（手动构建，发布到 ghcr.io/umbrella0162/napcat）

在 `Umbrella0162/NapCatQQ` fork 内自包含地构建 x86_64 镜像。镜像按官方
`mlikiowa/napcat-docker` 的构建历史逐条复刻，并在 CI 里用从官方镜像提取的
指纹基线 + 运行时行为断言把关。

**只手动触发**：`workflow_dispatch`，不跟随 push / PR。

## 一致性依据

官方镜像在本机已由 Nix 的 `dockerTools.pullImage` 固定成闭包：

```
mlikiowa/napcat-docker:v4.18.41
digest  sha256:b101b8968cc31ae428ec0f8e117386e4ced7773b2d8226ed9f7ddfcec55f1b7e
→ /nix/store/x1qn5yfbj034xx9ad68sshf8941vqdn1-docker-image-mlikiowa-napcat-docker-v4.18.41.tar
```

用同样参数重新求值 `pullImage` 会得到同一个 store 路径，说明该归档就是官方镜像本体。
它的 image config 带着完整构建历史，`entrypoint.sh` 与 `templates/` 直接从归档取出：

```
ENV          DEBIAN_FRONTEND=noninteractive TZ=Asia/Shanghai
FROM         ubuntu:22.04
RUN          apt-get install -y libnss3 libnotify4 libsecret-1-0 libgbm1 xvfb libasound2
               fonts-wqy-zenhei gnutls-bin libglib2.0-dev libdbus-1-3 libgtk-3-0
               libxss1 libxtst6 libatspi2.0-0 libx11-xcb1 ffmpeg unzip
               dbus-user-session curl jq gosu tzdata
RUN          useradd --no-log-init -d /app napcat
WORKDIR      /app
COPY         NapCat.Shell.zip entrypoint.sh /app/
COPY         templates/ /app/templates/
RUN          curl .../linuxqq_3.2.34-53644_${arch}.deb && dpkg -i --force-depends linuxqq.deb
RUN          chmod +x entrypoint.sh
             && echo "(async () => {await import('file:///app/napcat/napcat.mjs');})();" > /opt/QQ/resources/app/loadNapCat.js
             && sed -i 's|"main": "[^"]*"|"main": "./loadNapCat.js"|' /opt/QQ/resources/app/package.json
VOLUME       /app/napcat/config
VOLUME       /app/.config/QQ
ENTRYPOINT   ["bash", "entrypoint.sh"]
```

**运行模型**：容器主进程是 QQ（`gosu napcat Xvfb ... &` 后 `gosu napcat /opt/QQ/qq --no-sandbox`），
NapCat 由 QQ 的 Electron 主进程通过 `loadNapCat.js` 加载。所以镜像必须自带 QQ 与 Xvfb，
不能写成 `node napcat.mjs`。

## 文件

| 路径 | 作用 |
| --- | --- |
| `docker/Dockerfile` | 逐条复刻官方构建历史（唯一差异：`NapCat.Shell.zip` 由本仓库源码构建） |
| `docker/entrypoint.sh` | 官方镜像内原文件，逐字节一致（sha256 `493cbb76…`） |
| `docker/templates/*.json` | 官方镜像内 9 份 OneBot 模板，逐字节一致 |
| `docker/official-contract.json` | 从官方镜像提取的契约指纹基线 |
| `scripts/parity.py` | `extract` 提取指纹 / `compare` 比对 |
| `.github/workflows/docker.yml` | 手动触发：构建 → 校验 → 发布 |

## CI 流程（workflow_dispatch）

1. **build-shell**：`pnpm` 按 `build-ci.yml` 的 Shell target 构建，装运行时依赖后压成扁平
   `NapCat.Shell.zip`。
2. **image**：构建 `linux/amd64` 镜像后
   - 静态契约：`docker save` → `parity.py extract` → 与 `docker/official-contract.json` 比对
     （配置、环境变量、napcat 用户、entrypoint 与模板哈希、QQ 版本、命令与 apt 包）；
   - 运行时行为：zip 能解到 `/app/napcat`、`WEBUI_TOKEN` 生成正确的 `webui.json`、
     `MODE=ws` 生成的 `onebot11.json` 等于官方 ws 模板、`/opt/QQ/qq` 进程真实存在。
3. **publish**：推 `ghcr.io/umbrella0162/napcat`
   - tag `v4.18.42` → `4.18.42`、`4.18`、`latest`、`sha-<7位>`
   - 其它 ref → `main`、`sha-<7位>`

## 本地重新生成基线

```bash
# 官方归档（本机 Nix 闭包，无网也可）
ARCHIVE=/nix/store/x1qn5yfbj034xx9ad68sshf8941vqdn1-docker-image-mlikiowa-napcat-docker-v4.18.41.tar
python3 scripts/parity.py extract "$ARCHIVE" -o docker/official-contract.json

# 比对某个已构建镜像（需 docker 或 podman 可 save）
podman save my-napcat:local -o ours.tar && \
  python3 scripts/parity.py extract ours.tar -o ours.json && \
  python3 scripts/parity.py compare docker/official-contract.json ours.json
```

## 部署（与官方 compose 完全兼容）

只改 `image:` 一行，环境变量与卷挂载点不变：

```yaml
services:
  napcat:
    image: ghcr.io/umbrella0162/napcat:latest   # 官方是 mlikiowa/napcat-docker:latest
    restart: unless-stopped
    environment:
      MODE: ws
      WEBUI_TOKEN: replace-with-your-own-token
    ports:
      - "127.0.0.1:6099:6099"
      - "127.0.0.1:3001:3001"
    volumes:
      - ./data/qq:/app/.config/QQ
      - ./data/config:/app/napcat/config
      - ./data/plugins:/app/napcat/plugins
```

## 已知边界

- **只做 amd64**：`QQ_ARCH` 已参数化，扩 arm64 时改 `platforms` 与 `QQ_ARCH` 即可。
- **apt 版本会漂移**：基础镜像是滚动的 `ubuntu:22.04`，逐层 hash 不可能与官方相同；
  断言的是配置/文件/依赖齐全性，不是层 hash。
- **QQ 包来自 `qqdl.gtimg.cn` 的 beta 路径**（与官方同一 URL）；路径失效时改 `QQ_VERSION_PATH`。
- 许可证：上游 *Limited Redistribution License for NapCat* 要求分发附许可证全文并注明来源、
  不得商用。本仓库不复制许可证文件（与官方镜像保持一致），对外分发时请自行确认合规。