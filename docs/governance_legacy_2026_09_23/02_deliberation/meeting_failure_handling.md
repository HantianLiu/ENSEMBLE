# Meeting Failure Handling

状态：PARTIALLY CONFIRMED

## Representative API Unavailability — CONFIRMED

当一次需要 Representative 响应的正式调用发生 provider/API connectivity failure：
- 初始失败后允许 3 次 retry；
- 3 次 retry 后仍不可达，记录 `REPRESENTATIVE_UNAVAILABLE`；
- 会议状态改为 `PAUSED`；
- 不缩减 roster、不临时取消该代表投票权、不以 quorum 替代；
- Human 决定恢复方式。

会议恢复时应从最近的 durable state 继续，而不是重新生成此前已冻结文书。

## Abstention — CONFIRMED

议事 ballot 不允许 abstain。

## 仍未定案

以下仍需明确：
- API 可达但返回 schema-invalid ballot；
- 模型明确拒绝选择合法选项；
- tool failure 与 model failure 的区分；
- retry 是否使用相同 prompt、修正版 prompt 或相同随机种子。
