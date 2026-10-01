# Representative 文件访问控制与足迹

状态：CONFIRMED

Representative 不拥有会议工作区的任意文件系统访问权。其可见文件仅能由当前阶段的上下文
装配器提供，并限于：共同规则、本人 persona runtime、当前阶段协议、当前公开状态，以及制度
明确允许的本人私有记录。

上下文装配器必须在读取前执行路径授权。下列访问必须拒绝：

- 其他 Representative 的目录或以其他 Representative ID 命名的私有记录；
- `identity_private`、`chair_private`、`human_private`、`audit_private` 或未授权的
  `governance_private` 内容；
- 以符号链接、路径跳转或错误分类方式离开获准 compartment 的路径；
- 未来阶段协议、其他 persona runtime 或完整治理仓库。

每一次获准读取须写入会议内的追加式哈希链账本，至少记录 Representative ID、当前阶段、
上下文角色、会议内相对路径、文件字节数和内容 SHA-256。被拒绝的尝试也须在读取内容前写入
账本，并记录请求路径和拒绝原因。账本位于
`governance_private/representative_file_access.jsonl`，不得注入任何 Representative 的上下文。

当前 provider API 不向模型开放自主文件工具。因此“获准读取”表示 Orchestrator 将该文件内容
装入了 Representative 的上下文，能够证明该 Representative 实际被展示了什么；它不表示模型
主动选择浏览该文件。当前实现中的拒绝记录通常表示上下文装配路径错误，而不是模型自行发起
了文件请求。未来若开放模型文件工具，该工具必须复用同一授权与账本边界，不能建立旁路。
