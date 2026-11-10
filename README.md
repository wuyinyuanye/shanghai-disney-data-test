# Shanghai Disneyland 排队数据 24 小时测试

本仓库用于**独立云端采样**，不依赖用户的 Mac 网络，也不是微信小程序。

## 测试窗口

- 开始：2026-11-10 12:05 中国标准时间（UTC+8）
- 结束：2026-11-11 12:05 中国标准时间（UTC+8）
- 计划频率：每 5 分钟执行一次（GitHub Actions 定时调度存在延迟或漏跑，不保证精确执行 288 次）
- 测试范围：**仅上海迪士尼**，ThemeParks.wiki + Queue-Times 两家第三方
- 每次检查独立保存 2 份原始 JSON 与 1 份对照报告，GitHub Actions Artifacts 保留 7 天
- 到期后工作流将停止请求数据，并尝试用 GitHub Actions 授权令牌自动禁用自身。即使禁用失败，脚本的时间窗口保护仍会阻止继续调用上游 API

## 测试的指标

1. **接口可用性**：两个 API 各自的成功次数、失败类型、HTTP 状态、请求耗时
2. **数据一致性**：同一轮里匹配到的项目数量、开闭状态是否一致、排队分钟数完全一致的比例
3. **时间覆盖与同步变化**：通过对比连续采样文件分析等待分钟数的变化、潜在的采集空窗和落后于另一个数据源的情况
4. **对照局限**：**两个第三方一致，不等于与迪士尼官方 App 一致。** 若需要检验官方同步延迟，必须在同一时间获取并记录官方 App 的显示数据

不要把 ThemeParks.wiki 的单项目 `lastUpdated` 与 Queue-Times 的 `last_updated` 当作意义完全相同的时间；保存它们是为后续分析，不能直接按时间差判定某一方不准确。

## 查看结果

- [GitHub Actions 运行历史](https://github.com/wuyinyuanye/shanghai-disney-data-test/actions/workflows/shanghai-disney-24h.yml)
- 打开任一运行记录，在 **Summary** 查看本次结果，在 **Artifacts** 下载 `shanghai-disney-sample-<run_id>` 压缩包
- 压缩包：`themeparks_raw.json`、`queue_times_raw.json`、`report.json`，若某个接口失败，其原始文件可能缺失，但错误仍记在 `report.json`

## 文件

- `monitor.py`：标准库 Python 采集与逐项比对、失败重试及时间窗口控制
- `config.json`：固定的采样开始/结束时间
- `.github/workflows/shanghai-disney-24h.yml`：每 5 分钟云端采样、归档和到期自动停用

## 限制

正式商用前，需要核查第三方的商业使用条款和数据源稳定性。本仓库不能证明迪士尼对第三方上游接口的授权，也没有验证官方 App 与第三方的同屏一致性。
