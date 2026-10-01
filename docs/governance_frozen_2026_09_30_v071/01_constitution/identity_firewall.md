# 身份防火墙（Identity Firewall）

状态：CONFIRMED

本文件是 Project_ENSEMBLE 反博弈体系的一部分；完整原则见 `anti_gaming_firewalls.md`。

## 1. 隐藏映射

系统维护代表注册表：

`Representative ID -> model / provider / harness / persona / prompt version / reasoning setting / runtime configuration`

该注册表在议事会议中不得暴露给 Representative。

## 2. Representative 可见信息

Representative 只可见当前阶段完成任务所需的信息，例如：
- 自己当前必要的运行角色；
- 当前有效的临时 Representative ID；
- 当前阶段允许公开的文书、证据与状态；
- 当前阶段的合法 action space。

Representative 不因“完整制度透明”而自动获得未来阶段信息。

## 3. Representative 不可见信息

不得访问：
- representative registry；
- 其他 persona 文件；
- 其他 Representative 的 system/persona prompt；
- provider credentials；
- 其他 Representative 的 hidden reasoning；
- Chair 私有观察；
- governance-private scoring / ranking；
- selection rationale；
- audit-private data；
- 尚未进入阶段的程序规则。

## 4. 揭盲时点

顺序必须为：

1. 议事结束；
2. resolution freeze；
3. minority/advisory material freeze；
4. 会后审计申请窗口关闭；
5. audit petitions freeze；
6. 身份映射才可提供给授权 Audit 层。

审计申请本身必须在盲态下形成。

## 5. 会议本地身份

Representative ID 每场会议重新生成。

不得让同一真实配置长期对应同一可见 ID。
