# 水电费管理 · 设计契约

> 单用户本地 Web 工作台,管理房东总表与租户分表的抄表、缴费与费用。
> 本文件是 UI 的唯一设计契约:改样式前先读它,新增页面照它的配方做。

---

## Design Tokens

```yaml
colors:
  bg:        "#f7f8fa"   # 页面底
  surface:   "#ffffff"   # 卡片/表格
  surface-2: "#f4f6f9"   # 表头/代码块
  ink:       "#111827"   # 主文字
  ink-2:     "#475467"   # 次文字
  muted:     "#98a2b3"   # 弱文字
  line:      "#e4e7ec"   # 边框
  line-2:    "#d0d5dd"   # 强边框
  brand:     "#3459e6"   # 品牌蓝,只用于主按钮/当前项/关键强调
  brand-ink: "#1e3a9c"
  brand-soft:"#eef2ff"
  positive:  "#067647"   # 在用/已结清
  positive-soft: "#ecfdf3"
  caution:   "#b54708"   # 未收款/空置/提示
  caution-soft: "#fffaeb"
  negative:  "#b42318"   # 删除/错误
  negative-soft: "#fef3f2"

typography:
  family: '"Inter", "Segoe UI", "Microsoft YaHei", system-ui, sans-serif'
  mono: 'ui-monospace, "Cascadia Mono", Consolas, monospace'
  scale: [11, 12, 13, 14, 16, 20, 26]
  weight: {regular: 400, medium: 500, semibold: 600}
  # 数字列一律 tabular-nums

layout:
  unit: 4px
  spacing: [4, 8, 12, 16, 20, 24, 32]
  sidebar: 228px
  sidebar-collapsed: 56px
  content-max: 1240px
  breakpoints: [640, 860, 1100]

shapes:
  radius: {sm: 4px, md: 6px, lg: 10px}
  border: 1px

motion:
  fast: 120ms
  ease: cubic-bezier(.2,.8,.2,1)
```

### Usage Guidance

- 间距一律 4px 倍数;优先 8/12/16。
- 品牌蓝 `#3459e6` 只给主按钮、当前导航项、链接;**不要**给普通图标、标题、统计卡上色。
- 不用彩色渐变、不用大阴影、不用大圆角卡片。分隔靠 1px 边框与背景色差。
- 状态色(绿/橙/红)只用于语义标签与提示条,不用于装饰。
- 文字最多三级:标题 16-20px、正文 13-14px、辅助 11-12px。

---

## Component Recipes

### Layout

- 固定左侧栏 + 右侧内容区;窄屏(<860px)侧栏改横向顶部。
- 内容区最大 1240px,居中,左右 padding 28px。
- 页面头:标题 + 一句说明,下方 1px 分隔线。
- 卡片之间 14px 间距;卡片内 padding 18px。

### Navigation

- 侧栏分组标签:11px 大写弱文字。
- 导航项:图标 + 文字,当前项浅蓝底 + 左侧 3px 蓝条 + `aria-current="page"`。
- 装饰图标 `aria-hidden="true"`。
- 侧栏底部退出登录,与上方用 1px 分隔。

### Buttons

- 主按钮:蓝底白字;次按钮:白底灰边框;危险按钮:白底红字红框。
- 小按钮用于表格行内操作,高度约 24px。
- 所有按钮 hover 变深,`:focus-visible` 显示 2px 蓝环。

### Forms

- label 在控件上方,11-12px 次文字,带 `for`/`id` 配对。
- 输入框 4px 圆角,1px 边框;聚焦蓝边 + 2px 浅蓝光晕。
- 筛选器用 `auto-fit minmax(160px,1fr)` 网格,一行放不下自动换行。
- 只读字段(自动计算的金额)用灰底。

### Feedback

- 成功/错误提示条放页面头下方,左边 3px 语义色,带 `role="status"`。
- 空状态:表格内居中一句弱文字,不用插图。
- 危险操作(清空数据、删除)必须 `confirm()`。

### Data Display

- 表格:表头浅灰底 + sticky;行高约 30px;hover 浅灰。
- 数字列右对齐 + `tabular-nums`。
- 状态用 pill 标签:`在用`/`已结清`绿、`未收款`/`空置`/`已换下`橙、`总表`灰、`分表`蓝。
- 计量线详情统一用一张表:类型 / 线名 / 账期 / 用量 / 单价 / 应收 / 已收 / 欠款 / 状态。
- 统计卡:白底细边框,数字 20-26px,不加上色左边条以外的装饰。

### Login

- 全屏冷灰底,居中卡片,顶部 3px 品牌蓝边。
- 卡片内:品牌方块 → 标题 → 一句说明 → 密码框 → 主按钮 → 底部提示。
- 不用渐变背景、不用插画。

---

## Voice & Tone

- 按钮动词具体:「保存抄表」「确认换表」「清空所有数据」,不用「确定」「提交」。
- 提示直接给下一步动作,不只报错。
- 中文标点用半角逗号与句号。

## Do's & Don'ts

- Do:细边框、紧凑行高、信息密度、等宽数字。
- Don't:紫色渐变、大圆角、大阴影、三等分图标卡、居中大标题、Inter 之外的花哨字体。
- Don't:用 `transition: all`、无替代的 `outline: none`、无理由的 `autofocus`。

---

## 登录页设计理由

登录是唯一未登录可见的页面,承担品牌第一印象。用纯色背景 + 单张卡片 + 顶部蓝边,
是为了在没有导航壳的情况下依然与工作台视觉一致;去掉渐变和插画,避免"营销页"气质 ——
这是内部工具,不是落地页。
