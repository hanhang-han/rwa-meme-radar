# CliperX `/dashboardv2/` 独立发布说明

此发布把 V2 页面放在 `https://cliperx.com/dashboardv2/`，静态文件独立存放于服务器的 `/opt/memedashboard/web-v2/dist`。现有 `/dashboard/` 前端目录不移动、不覆盖。两个入口共用当前 Python API（127.0.0.1:8010）、持久投影和采集事实库；V2 不会另开采集器、复制数据库或增加上游调用。前端在 `/dashboardv2/` 下请求同源 `/dashboardv2/api/`，Nginx 将此路径转发到现有 `/api/`。

发布脚本是 [deploy-dashboard-v2.sh](../scripts/deploy-dashboard-v2.sh)，Nginx 路由模板是 [dashboardv2-nginx.conf](../scripts/dashboardv2-nginx.conf)。运行前先看本地 `bash -n scripts/deploy-dashboard-v2.sh` 和 `scripts/deploy-dashboard-v2.sh --prepare-only` 的结果；前者检查语法，后者执行前端、后端测试并构建发布包，但不连接服务器。正常发布用 `scripts/deploy-dashboard-v2.sh`。

脚本只打包 `server-py/app/state.py`、`server-py/app/theme_map.py` 和 V2 静态文件。远程会先校验当前 `state.py` 是否与 Git 标签 `dashboard-before-v2-20260929` 完全一致，并校验 Python 依赖文件、API 就绪状态、PM2 服务和实际生效的 Nginx 配置。如果线上代码或站点配置已有额外改动，它会停止，不覆盖未知版本。首次发布只会向 `cliperx.com` 唯一的 HTTPS server 块插入一个 V2 配置 include；发现已有 `/dashboardv2` 路由则停止，后续 V2 迭代需要单独审核更新方案。

切换时只短暂停止并重启 `pyradar-projection` 与 `pyradar`，不停止 `pyradar-worker`。V2 的 Nginx include 固定在 `/opt/memedashboard/nginx/dashboardv2.conf`；若该文件已存在，首次发布会停止，绝不覆盖。脚本保存原 `state.py`、可选旧 `theme_map.py`、旧 V2 静态目录和原 Nginx 站点文件到 `.releases/dashboard-v2-before-*`，然后先测试 Nginx 配置再平滑重载。发布后核验：本机和公网 API 就绪；公网 V2 HTML 与新包哈希一致且脚本资源可读；服务器本机完整数据及公网概览数据均含 `themeMap` 与 `importantChanges`；旧 `/dashboard/api/health/ready` 仍可访问；采集进程 PID 未变化。任一步失败会尝试自动恢复上述文件并重启 API/投影，明确报告回滚是否完整。实际完成与否必须以脚本最终输出和公网浏览器验收为准。

如需**发布成功后手动退回旧站点**，先读取本次脚本打印的 `Rollback directory`，再在服务器上执行以下步骤。`BACKUP` 应换成本次发布对应的精确目录；不要使用数据库旧副本覆盖当前研究库。

```bash
cd /opt/memedashboard
BACKUP=/opt/memedashboard/.releases/dashboard-v2-before-具体时间戳
export PATH="/www/server/nodejs/v22.22.0/bin:$PATH"
sudo -n cp -p "$BACKUP/cliperx.com.conf" /www/server/panel/vhost/nginx/cliperx.com.conf
sudo -n /www/server/nginx/sbin/nginx -t
sudo -n /www/server/nginx/sbin/nginx -s reload
rm -f nginx/dashboardv2.conf
pm2 stop pyradar-projection pyradar
cp -p "$BACKUP/state.py" server-py/app/state.py
if [ -f "$BACKUP/theme_map.py" ]; then
  cp -p "$BACKUP/theme_map.py" server-py/app/theme_map.py
else
  rm -f server-py/app/theme_map.py
fi
pm2 restart pyradar-projection pyradar
if [ -d "$BACKUP/dist" ]; then
  mv web-v2/dist "$BACKUP/failed-dist"
  mv "$BACKUP/dist" web-v2/dist
fi
curl -fsS https://cliperx.com/dashboard/api/health/ready -o /dev/null
```

首次发布前服务器尚无 `web-v2/dist`，所以手动回滚后 V2 静态文件可留作离线恢复材料；Nginx 已不再公开它。检查 `pm2 pid pyradar-worker` 仍为运行状态。旧 `/dashboard/` 页面从始至终使用原 `web/dist`。

## 首次上线记录（2026-09-29）

本机开发提交为 `6fd985c`，后续发布脚本修正提交为 `66573e8`、`41af1c8`。旧版恢复标签 `dashboard-before-v2-20260929` 指向 `a8db1da`。本次成功发布的服务器代码与 Nginx 回滚目录为 `/opt/memedashboard/.releases/dashboard-v2-before-20260929T040224Z-581580`。

上线验收：`/dashboard/`、`/dashboardv2/`、V2 健康接口均返回 200；公网 V2 概览与服务器本机完整投影都含主题地图和重要变化字段；桌面 Chrome 加载真实完整快照后显示主题气泡、名称线索和最近观察，中文与英文切换正常，手机无横向溢出，页面脚本没有报错。采集进程 PID 在发布前后未变化。

主题地图只展示当前达到 A 级的股票代币配对池，证据过期时数量会下降。首次验收时 A 级地图样本在不同采样点从 26 到 37 个合约变化；这不等于资产被删除。当前新核验事件为 0 时，页面会清楚标注并展示“最近观察”作为另一类记录。首页“数据达标的活跃 Meme”当时为 0，因为该指标同时要求新鲜报价和可比的资产总流动性，不能用单池流动性冒充。成交突然活跃、买入地址及地址聚类尚未发布，待采样覆盖和地址证据满足定义后再上线。
