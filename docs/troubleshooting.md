# 常见问题排查

## 群列表为空

可能原因：

- 微信 PC 版未登录。
- `wxdump_work/conf_auto.json` 不存在或账号未选择。
- 尚未点击“同步”生成 `merge_all.db`。
- 微信版本更新后 PyWxDump 暂不支持当前偏移量。

处理方式：

1. 确认微信 PC 版已登录。
2. 在设置向导中重新检测账号。
3. 使用管理员身份启动程序后再次检测。
4. 点击“同步”。
5. 如果仍失败，查看程序日志中的 PyWxDump 错误。

## SQLite database is locked

程序不会直接写微信原始数据库。同步时会先复制微信数据库快照到临时目录，解密输出到 `merge_all.pending.db`，校验通过后再替换正式 `merge_all.db`。

如果仍看到锁竞争：

- 等待当前同步任务完成后重试。
- 确认没有其他 SQLite 工具打开 `merge_all.db`。
- 重启本程序，释放旧连接。
- 重启微信 PC 版，让 WAL 内容刷盘后再次同步。

## 同步成功但消息不是最新

微信运行时最新消息可能停留在 WAL 文件中，或者微信刚滚动到新的 MSG 数据库文件。

建议：

1. 等待 1 到 3 分钟后再次点击“同步”。
2. 重启微信 PC 版，强制刷新数据库。
3. 确认 `wxdump_work/<wxid>/merge_all.db` 的最新消息时间。

## AI 总结超时

长群聊会先经过清洗和 token 估算；超过预算时自动进入 Map-Reduce 分块总结。前端会显示“读取中、清洗中、分块总结、合并摘要、保存”等状态。

如果仍超时：

- 减少最近消息条数或缩短时间范围。
- 在设置中增大请求超时。
- 换用上下文窗口更大的模型。
- 检查代理或网络是否稳定。
- Ollama 用户确认本地模型已拉取且服务在 `localhost:11434` 正常运行。

## AI 返回 context length exceeded

这通常表示模型真实上下文小于配置预估，或自定义 Prompt 过长。

处理方式：

- 减少消息条数。
- 缩短自定义 Prompt。
- 换用更大上下文模型。
- 保留默认清洗和分块逻辑，不要直接绕过 `provider.summarize()`。

## 自动检测微信账号失败

如果提示“检测到微信进程但无法读取密钥”：

- 右键以管理员身份运行程序。
- 确认微信已完全登录，不是扫码等待状态。
- 如果已经管理员运行仍失败，可能是微信版本不受当前 PyWxDump 偏移量支持。

## 敏感文件安全

以下文件不得提交：

- `wxdump_work/`
- `.env`
- `*.db`
- `*.db-wal`
- `*.db-shm`
- `~/.wechat-summary/config.json`

提交前执行：

```powershell
git status --short
git status --ignored --short wxdump_work .env config.json
```
