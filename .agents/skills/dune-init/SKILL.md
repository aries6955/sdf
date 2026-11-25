---
name: dune-init
description: 用户点名 dune-init 时才执行。初始化时用官方语法器按区间删掉注释和单独成句的说明字符串，并留下写明改法的检查。同一检查接到 Cursor、Claude Code、Codex、Pi 的结束钩子上，提交钩子再拦一次。空项目或新建后端接入 1 个官方 Lint；老项目默认只列出存量命中，用户要求清零时才按模块分批改。
disable-model-invocation: true
---

# dune-init

只在用户明确叫 `dune-init` 时执行。做完就停，不顺手改业务。

日常开发中，不要为了通过检查而放宽或修改已有 Lint 规则。只有用户再次叫 dune-init 追加规则时，才改规则文件。

两种调用：

- 初始化：源码里不再有注释。空项目或新建后端同时打开高危 API 和目录边界。
- 加规则：用户用一句话指出反复写错的坑，把它写进现有 Lint。

完成只看两件事：动过的文件能被原来的语法器再解析一次，项目原来的构建仍然通过。做不到就撤回这一步。

## 存量纪律

已有代码里的命中一律先列清单，不擅自改。只有用户在这条指令里要求清零存量技术债或清理旧代码时，才按模块分批改、每批验构建、失败撤回该批。未要求时不改那些文件，也不把检查设为失败。

空项目、或用户指定在现有项目旁新建的后端目录，才算「新」。新建目录没点名旧目录时，旧目录的注释和原有 Lint 保持不动。

## 动手前

先跑 `git rev-parse --is-inside-work-tree` 判断是不是 git 仓库。后面所有还原和增量判断都按这个结果二选一，不要混用。

git 仓库：工作区不干净就先切新分支。非 git 项目：动手前把要改的文件复制到 `.dune-backup/<时间戳>/`，全部构建通过后再删掉这个目录。

先跑通项目已经在用的构建。构建本来就失败就停，把失败告诉用户。

已有 Lint 和目录保持原样，只追加。不升级大版本，不新增第二份配置，不搬目录，不改依赖目录、生成物和压缩产物。若原构建命令耗时极长（如完整移动端打包），分批阶段可先做语法与类型复解析，收尾再统一跑构建。

## 删注释

没有注释是这一次必须做完的。

用该语言的官方词法或语法接口找出注释的字节区间，从原文件里删掉这些区间。其它字节保持不动。禁止用正则、整行替换或整文件重排来删注释。

这些区间留下，删掉会改变编译结果：

- 首行 shebang
- Python 第 1 或第 2 行的编码声明
- Go 的 `//go:`、`//+build`、`//export`、`//line`，以及紧挨 `import "C"` 前面的 cgo 前言
- TypeScript 的 `/// <reference`

每个文件删完，立刻用同一解析器再解析。失败就还原该文件。按目录分批，每批之后编译这一批碰到的包。全部完成后跑一次原来的构建。构建失败就撤回最后一批，不手工改逻辑：git 仓库用 `git checkout -- <这一批的文件>`，非 git 从 `.dune-backup/<时间戳>/` 拷回。

- Go：`go/parser` 取注释区间，跳过上面的编译指令，从后往前删字节，再 `ParseFile`。
- Python：`tokenize` 只删除 `#` 注释，它不把三引号当注释。再用 `ast` 删除单独成句的字符串，包括模块、类、函数的文档字符串，以及函数体里丢掉的 `"""说明"""`。赋给变量、传入参数、作为返回值的字符串保留。删完用 `compile` 复核。
- JS/TS：用项目里已经安装的 TypeScript 或 Babel 取注释区间，从后往前删原文，再解析。不要使用 `removeComments: true`。JSX/TSX 中的 `{/* ... */}` 需连同外层空 `{}` 一并移除。单独成句、结果被丢掉的字符串同样删除。项目没有解析器就不删这一语言，并说明缺解析器。空项目或新建后端要先装上该语言的官方 Lint，解析器随它而来，再删注释。空项目同时打开未使用变量规则，避免把说明存进一个不用的名字。
- 其它语言：只用标准库词法器按区间删。没有词法器就跳过该语言并说明。

用仓库里真实含有 `https://` 或 `#` 颜色的文件确认这些字符还在。

## 可选加固

只在空项目或新建后端里打开。老项目不为了这两项新装 Lint，也不把业务代码搬进新建目录来换通过。

| 组 | 规则 | 例外目录 | 默认 |
|---|---|---|---|
| 高危 API | `eval`、`exec`、`new Function`；把错误扔掉：`except: pass`、空 `catch`、显式丢掉的 `err`；拖时间：`sleep`、`time.Sleep` | 仓库里已经负责底层封装的目录；没有就建一个名字能看懂的作为唯一例外 | 例外目录之外才算违规，只列命中 |
| 目录边界 | 任何一边 import 另一边都失败，只有这一条 | 沿用现有目录名，只建两个名字本身能说明职责的 | 先数两边 import，已有一边在引用就不加规则、不搬代码 |

两组都用刚接入的那 1 个官方 Lint 的现成规则（JS/TS 用 ESLint，Python 用 Ruff，Go 用 golangci-lint），报错文案写明应该改成哪种写法。已有代码且没有导入检查时，不要手写扫描器。

## 检查与钩子

检查脚本固定落到 `scripts/check_comments.py`，分类方式与删除时相同：可删注释、或上面说的单独成句字符串，都失败。报错文案固定为：禁止用注释解释临时补丁或提醒下一个 Agent，请通过命名、类型或测试表达意图并直接修复根因。`--diff` 模式只看新命中：git 仓库用 `git diff` 的新增行和未跟踪源文件；非 git 项目与 `.dune-baseline.json` 比对，只报基线之外的行，基线不存在就提示先跑 `--snapshot`，不要退化成全量报错。`--snapshot` 遍历源码扩展名，跳过依赖目录和生成物，写 `相对路径 + 命中行内容哈希`，行号漂移不影响判定。脚本自己在运行时判断走哪条，钩子不用改。

这一个文件是三处钩子的唯一实现：Agent 结束钩子、`git commit` 前置钩子、手动跑。空项目那条串行检查命令也调它。命令调用本机已有的 `python`，不写 bash 脚本。脚本若放在会被 `go build ./...` 编进去的目录，第一行保持 `//go:build ignore`，用 `go run -tags ignore` 运行。

钩子只在 `--diff` 已经能在当前改动上跑通之后才装。已有配置只追加这条钩子，不覆盖别的键。

编辑器红线不会替 Agent 跑这条检查，所以结束钩子必须接上。各家语义不同，用错等于放行：

| 工具 | 文件与事件 | 拦下的返回 | 坑 |
|---|---|---|---|
| Cursor | `.cursor/hooks.json` 的 `stop` 和 `subagentStop` | 退出码 0，stdout `{"followup_message":"..."}` | 非零退出默认放行；自动追问默认最多 5 次 |
| Claude Code | `.claude/settings.json` 的 `Stop` 和 `SubagentStop` | 退出码 0，stdout `{"decision":"block","reason":"..."}` | 别输出 `continue: false`，那会直接结束；连续拦下默认最多 8 次 |
| Codex | `.codex/hooks.json` 的 `Stop` 和 `SubagentStop` | 退出码 0，stdout 同上的 `{"decision":"block","reason":"..."}` | 别输出 `continue: false`；项目钩子要等用户在 `/hooks` 里信任之后才会跑 |
| Pi | `.pi/extensions/dune-comments.ts` 的 `agent_before_settle` | 返回 `{entries, continue: true}`，注入 `display: false` 的 `custom_message` | 没有内建次数上限，必须自己计数；`continue: false` 不会中止自然运行；Escape 中止时 continue 被抑制 |

Pi 没有上限，模板自带计数器，不要去掉：

```ts
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MAX = 3;
export default function (pi: ExtensionAPI) {
  let strikes = 0;
  pi.on("agent_before_settle", async (event, ctx) => {
    if (strikes >= MAX) return;
    const r = await pi.exec("python", ["scripts/check_comments.py", "--diff"], { cwd: ctx.cwd });
    if (r.code === 0) return;
    strikes++;
    return {
      entries: [...event.entries, {
        type: "custom_message",
        customType: "dune-comments",
        content: `${r.stdout}\n禁止用注释解释临时补丁或提醒下一个 Agent，请通过命名、类型或测试表达意图并直接修复根因。`,
        display: false,
      }],
      continue: true,
    };
  });
}
```

提交钩子只在 git 仓库里装，给每一次 `git commit` 兜底，包括没有结束钩子的工具。已有钩子就追加，不要换掉原来的 hooks 路径。命中时用非零退出码拒绝提交，不跑完整构建。Pi 不接管 git，这一处与它无关。非 git 项目跳过这一整节，结束钩子是唯一兜底，必须接上。

## 把一句话写进 Lint

1. 在已经在用的 Lint 里追加一条规则。还没有 Lint 的空项目，先按初始化接入那 1 个官方 Lint，再追加。报错文案写清错在哪里、应该改成什么。
2. 放一个最小违规样本，确认输出就是这句文案，然后删掉样本。
3. 跑原来的构建。
4. 仓库里已有的同类写法按存量纪律处理。
