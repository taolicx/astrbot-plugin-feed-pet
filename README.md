# AstrBot 喂食插件

这是一个可直接作为仓库发布的 AstrBot 插件，用来给虚拟宠物投喂食物。

当前版本除了基础喂食以外，还支持：

- 自定义食物种类
- 自定义每种食物增加多少饱食度
- 自定义成功进食后增加多少好感度和心情值
- 可配置喜欢、一般、不喜欢三种食物偏好
- 宠物吃撑后会拒绝继续进食
- 宠物吃到不喜欢的食物会直接拒绝
- 支持按用户限制投喂权限
- 支持投喂冷却时间
- 成功或拒绝时可以调用 AstrBot 当前 LLM 生成反应
- 饱食度会随时间自然衰减，心情值会逐步回稳

## 命令

- `投喂 食物名`
- `喂食 食物名`
- `喂食状态`
- `饱食度`
- `好感度`
- `心情值`
- `喂食帮助`
- `喂食重置`

示例：

```text
投喂 苹果
喂食 小鱼干
喂食状态
好感度
```

## 行为规则

1. 插件按会话保存宠物状态。
2. 每个会话都有独立的饱食度、好感度和心情值，群聊和私聊互不影响。
3. 当当前饱食度大于等于 `refuse_threshold` 时，宠物会因为太撑而拒食。
4. 当食物被标记为 `dislike` 时，宠物会直接拒绝，不增加饱食度。
5. 成功进食会增加饱食度，并按食物配置增加好感度和心情值。
6. 未知食物、讨厌食物和吃撑强喂都可以按配置扣除好感度和心情值。
7. 同一用户在同一会话里会受 `feed_cooldown_seconds` 限制。
8. 如果配置了 `allowed_user_ids` 或 `blocked_user_ids`，会先做权限判断再允许投喂。

## 安装方式

把整个目录复制到 AstrBot 插件目录下，例如：

```text
AstrBot/data/plugins/astrbot_plugin_feed_pet
```

推荐最终结构：

```text
astrbot_plugin_feed_pet/
├─ main.py
├─ metadata.yaml
├─ _conf_schema.json
└─ README.md
```

复制完成后重启 AstrBot。

## 配置说明

### 基础状态

- `pet_name`: 宠物名字
- `initial_satiety`: 初始饱食度
- `max_satiety`: 最大饱食度
- `refuse_threshold`: 拒食阈值
- `satiety_decay_per_hour`: 每小时饱食度衰减
- `initial_favorability`: 初始好感度
- `max_favorability`: 最大好感度
- `initial_mood`: 初始心情值
- `max_mood`: 最大心情值
- `mood_recovery_per_hour`: 每小时心情回稳

### 投喂控制

- `feed_cooldown_seconds`: 投喂冷却秒数
- `require_admin_to_feed`: 群聊仅管理员可投喂
- `allowed_user_ids`: 允许投喂用户白名单
- `blocked_user_ids`: 禁止投喂用户黑名单

### 拒绝惩罚

- `unknown_favorability_penalty`
- `unknown_mood_penalty`
- `disliked_favorability_penalty`
- `disliked_mood_penalty`
- `too_full_favorability_penalty`
- `too_full_mood_penalty`

### LLM 反应

- `llm_enabled`: 是否启用 LLM 反应
- `llm_provider_id`: 指定使用哪个 provider，留空则跟随当前聊天模型
- `llm_prompt`: 宠物反应系统提示词

当前默认提示词已经改成“稳重、克制、稍微嘴硬的中年男性”口吻，不再默认走卖萌宠物风。如果你的人设不是这一类，再去面板里调整 `llm_prompt` 即可。

如果没有可用 provider，插件会自动回退到内置模板文案，不会影响主流程。

### 食物列表

`food_items` 里每一项都支持：

- `name`: 食物名
- `aliases`: 别名，可用逗号或换行分隔
- `satiety_gain`: 吃下后增加的饱食度
- `favorability_gain`: 吃下后增加的好感度
- `mood_gain`: 吃下后增加的心情值
- `preference`: `like` / `neutral` / `dislike`
- `note`: 提供给 LLM 的食物备注
- `accept_hint`: 吃下时的附加文案
- `refuse_hint`: 拒绝时的附加文案

## 数据存储

插件会把运行时状态保存到 AstrBot 为该插件分配的数据目录中，文件名为：

```text
feed_state.json
```

它会保存：

- 每个会话的饱食度、好感度、心情值
- 最近一次进食结果
- 成功/拒绝次数
- 每个用户在当前会话中的投喂冷却记录

## 适合的扩展方向

- 按用户维护独立好感度，而不是整个会话共用一套
- 给不同食物绑定表情包、图片或语音
- 给不同群设置不同宠物名字和食谱
- 增加成长阶段、体型变化或亲密事件
