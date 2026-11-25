import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// dune-init 注释红线:agent 结束前跑 scripts/check_comments.py --diff,
// 有新命中就注入一条 display:false 的 custom_message 要求修复。没有内建
// 次数上限,这里自带计数器(MAX 次后放行,避免死循环),不要去掉。
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
