# 飞书卡片插件开发规范

本目录仅维护 AstrBot 飞书卡片插件，机器人对话与模型配置由 AstrBot 管理。

- 修改前阅读 README.md、检查 Git 状态与 diff，保留未提交改动。
- 不改 AstrBot 上游核心，不把消息理解改成关键词路由或硬编码回答。
- 本机只开发和检查，禁止连接生产飞书应用或启动第二条长连接。
- Python 检查使用 -B；卡片渲染测试可在本机运行，依赖 AstrBot 的运行测试在无网络容器中完成。
- 不把密钥、Token、聊天记录、业务结果或生产配置提交到仓库。
- 部署通过独立服务器运维工作区的 deploy-plugin.sh，不将主机运维放进插件仓库；生产插件位于 /srv/apps/astrbot/state/data/plugins/astrbot_plugin_feishu_agent_card。
- 不自动提交、推送、发布版本或发送测试消息。插件改动更新 CHANGELOG.md。
