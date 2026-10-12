# Evol：可恢复 Runtime

Evol S1 提供本地、单 Worker、顺序执行的固定计划 Runtime。任务从声明输入建立独立候选副本，以 SQLite 保存运行状态、文件操作意图、证据、预算和 Checkpoint，支持显式恢复、取消与结果发布门禁。

当前版本 0.1.0。原候选在 Windows / Python 3.12.10 下通过 57/57 测试和 32/32 场景；独立复验另修复了最终文件核验跨过截止时间后仍能发布的问题，当前包含 58 项 Runtime 测试，复验结果见下文。

## 当前能力

- 创建时冻结 JSON 计划、输入清单、策略与运行/工具版本；仅复制声明的普通文件。
- 内置 `read_file`、`replace_exact`、`assert_file_sha256`。精确替换要求非空、唯一匹配、内容确实变化，并保留未修改字节和 CRLF。
- Run、Step、Attempt、Operation 分层记录；步骤结果、Observation、预算结算、事件与 Checkpoint 在同一事务提交。
- 写入意图先持久化，恢复核对真实完整文件。效果已确认则补记录，未生效才经准入创建新尝试；冲突保留现场并停止。
- 状态根目录使用 OS 锁限制一个执行 Worker；取消和查询使用独立短事务。
- 持久化派发预算、每步尝试上限及 deadline。恢复不退回预算或延长截止时间；支持有界临时错误重试和时钟倒退检测。
- 最终重新核对声明文件、证据、取消和预算；只有 `completed` 状态中的已登记结果可交付。读取结果时重新检查工件完整性。

核心采用 Python 3.12+ 标准库，运行时无第三方依赖。包构建后端固定为 setuptools 80.9.0，首次安装可能需要下载构建依赖。

## 安装与三步演示

在本项目工作区打开 PowerShell：

```powershell
python -m venv .venv
$py = '.\.venv\Scripts\python.exe'
& $py -m pip install .

# 状态目录必须与 source 互不包含；每次创建生成一个新 run-id。
$state = 'docs/local/artifacts/s1/demo-state'
$created = & $py -m evol --state-root $state create --source examples/s1/source --plan examples/s1/plan.json | ConvertFrom-Json
$run = $created.run_id
& $py -m evol --state-root $state inspect $run
& $py -m evol --state-root $state start $run
& $py -m evol --state-root $state events $run --after-sequence 0
& $py -m evol --state-root $state result $run
```

演示在候选副本中将 `timeout=30` 改为 `timeout=60` 并核对完整摘要。源文件保留原始内容。结果中的 `files` 保存候选文件摘要；候选位置为 `<state-root>/runs/<run-id>/candidate/`。

计划示例见 [examples/s1/plan.json](examples/s1/plan.json)。`inputs` 声明可访问文件；每步必须包含唯一 `id`、已注册的 `tool` 和严格参数。`expected_from` 只能引用同文件的更早精确替换步骤。该摘要断言验证内容符合计划预期，业务正确性由使用者另行验证。

可以在 create 时用 `--policy policy.json` 覆盖策略，例如：

```json
{"max_dispatches": 2, "max_wall_seconds": 60}
```

默认最多 16 步、32 次派发预留、每步 2 次尝试、60 秒墙钟时间；单文件 1 MiB、输入总量 8 MiB、最多 32 个文件。策略创建后冻结。预留即计费；恰好用完最后一次且全部步骤完成仍可交付。

## 恢复、取消与查询

```powershell
# Worker 意外退出后显式恢复；先核对未决效果，不直接重放。
& $py -m evol --state-root $state resume $run

# 可以从另一终端提交。返回 cancellation_pending 表示请求已持久化。
& $py -m evol --state-root $state cancel $run
& $py -m evol --state-root $state inspect $run
```

`start` 仅接受 queued；`resume` 可续接非终态，终态只返回已有状态。取消不自动启动 Worker；Worker 已退出时，下一次 resume 只协调已开始的操作并终止。已派发写入可能完成，取消会阻止后续步骤和成功交付。取消保留候选修改，不自动回滚。

`conflict` 为终态，处理现场后须新建 Run。暂时无法核对的操作保持 recovering/stopping，返回退出码 8。inspect 显示状态、停止原因、预算、未决操作、最后事件、孤立 run 目录和可归属临时文件；只读命令不创建数据库或触发恢复。

CLI 标准输出为 UTF-8 JSON，标准错误为诊断。退出码：0 命令成功；2 参数/计划错误；3 Worker/数据库忙；4 failed/conflict；5 cancelled；6 budget_exhausted；7 状态/版本/工件完整性错误；8 协调未完成。

## 验证

安装后分别运行旧实验与新 Runtime 测试：

```powershell
& $py -m unittest -v test_runtime_lab
& $py tests/s1/run_acceptance.py
```

原候选补测为 57/57 通过、32/32 场景通过，无失败、错误或跳过。当前源项目独立复验为 58 项中 57 通过、1 项因 WinError 1314 失败、0 错误、0 跳过：唯一受阻项为真实符号链接夹具创建，失败发生在 Runtime 校验之前。该项沿用已核对的原候选补测证据，相关路径代码和测试保持一致；本轮 junction、越界、ADS、大小写重复及新增截止时间回归通过。不能将本轮结果表述为全套零失败。Windows 执行真实符号链接测试需要创建符号链接权限，可在具备该权限的 PowerShell 中运行测试。

旧实验本轮独立回归 10/10 通过。当前代码在新建虚拟环境中安装成功，确认包从 site-packages 加载；安装后的正常执行、真实进程中断恢复、取消和预算耗尽四种演示全部通过，源文件均保持不变。

崩溃测试使用独立 Python 进程的 `os._exit(77)`，覆盖意图提交后、临时文件完成/replace 前、replace 后/步骤提交前、步骤提交后及结果写出/完成事务前。故障点仅在测试驱动中提供，生产 CLI 不暴露故障注入参数。取消、deadline、预算、工件漂移、并发查询及恢复不重复写入另有测试。

旧 `runtime_lab.py` 和 `test_runtime_lab.py` 保留。单文件实验仍可直接运行：

```powershell
& $py runtime_lab.py --workspace <独立实验目录> apply config.txt --old 'timeout=30' --new 'timeout=60' --crash after_replace
# 上一条预期退出 77。
& $py runtime_lab.py --workspace <独立实验目录> recover
& $py runtime_lab.py --workspace <独立实验目录> inspect
```

旧实验的 `.runtime/operations.sqlite` 与新 Runtime 状态库完全独立。

## 能力边界

S1 支持已有普通文件的单次精确替换；多个文件可顺序处理，候选副本可能在中断时处于部分修改状态，不提供多文件原子事务。OS 锁限制协作 Worker，外部编辑器仍可能修改候选文件；Runtime 检测内容漂移并停止。

当前未验证断电、磁盘损坏、同权限恶意路径竞态、Linux/macOS、ACL/所有权/扩展属性或全新机器安装。临时文件、孤立目录及未发布结果保留诊断，未提供自动批量清理。

LLM、PR 审查、Git 版本输入、通用子进程与隔离测试、多 Agent、自进化及长期 Memory 尚未进入当前实现。
