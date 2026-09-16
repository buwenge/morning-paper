# 前端接入说明

本仓库 `frontend/` 目录整份收录了晨报专属的 4 个文件（组件与逻辑都是晨报
独有的，不含其它页面代码）：

- `frontend/components/MorningPaperPage.tsx` —— 晨报页整体（历史日期切换、
  板块渲染、打分/追后续入口、档案全文展开）
- `frontend/components/MorningPaperFoldCard.tsx` —— 聊天气泡里晨报注入文
  本被"折叠"成一张可点开的卡片
- `frontend/hooks/useMorningPaper.ts` —— 请求/接收 `get_morning_paper` /
  `morning_paper` 这对 WS 消息的状态管理
- `frontend/lib/morningPaperFold.ts` —— 把聊天正文里 `[MORNING_PAPER]...`
  这段文本切分出来，交给 `MorningPaperFoldCard` 折叠显示

生产 `xiaoyu-web/` 是一个更大的全站前端，晨报只是其中一个页面，下面几个
全站通用文件里各有几处晨报相关的改动点——这几个文件本身跟晨报无关的部分
体量很大，本仓库不整份收录，只按文件+行号摘出"在哪加了什么"：

## `src/lib/types.ts`

WS 消息类型定义里加了一去一回两行（放进各自的 union type）：

```ts
// 出站消息 union 里加一行：
| { type: "get_morning_paper"; date?: string }

// 入站消息 union 里加一行：
| { type: "morning_paper"; date: string; has_draft: boolean; items: MorningPaperItem[]; error: string | null; available_dates: string[]; feedback?: MorningPaperFeedback }
```

以及晨报专属的几个 interface（这几个直接搬进你自己项目的 types 文件即可）：

```ts
export interface MorningPaperItem {
  section: string;
  title: string;
  digest: string;
  url: string;
  url_warning: string;
  source: string;
  /** 晨报班查到的"真正的原文"链接（论文 arXiv 摘要页等），档案馆只给来源不抓正文，
   * 想看自己派 subagent。 */
  links?: MorningPaperLink[];
  archive_path?: string;
  archive_text?: string;
  /** 档案种类：html / pdf / x_tweet（推文档案只有那一条推文的文字）。 */
  archive_kind?: string;
  /** 档案文字版的 token 粗估（档案馆写文件时算好），用来标"直接读要占多少上下文"。 */
  archive_est_tokens?: number;
}

export interface MorningPaperLink {
  url: string;
  note: string;
  /** 按链接形状给的体量提示（"PDF·很耗 token，派 haiku"等）。 */
  hint?: string;
  /** arXiv 摘要页会顺手配出全文 PDF 链接。 */
  pdf_url?: string;
}

/** 打分反馈（morning_feedback.frontend_summary）：当前在追的条目 + 最近打了
 * 0 分（不感兴趣）的条目，最近的排前面。 */
export interface MorningPaperFeedback {
  following: MorningPaperFollowItem[];
  not_interested: MorningPaperNotInterestedItem[];
}

export interface MorningPaperFollowItem {
  id: string;
  title: string;
  url: string;
  issue_date: string;
}

export interface MorningPaperNotInterestedItem {
  title: string;
  section: string;
  issue_date: string;
}
```

## `src/components/DockBar.tsx`

底部导航加一个屏幕枚举值 + 一个入口按钮：

```ts
export type DockScreen = "home" | "chat" | "morning_paper" | "settings";
// ...
{ key: "morning_paper", label: "晨报", icon: (cls) => <IconNewspaper className={cls} /> },
```

## `src/app/page.tsx`

顶层页面把 `useMorningPaper()` 接进 WS 消息分发、屏幕切换状态机、以及
`MorningPaperPage` 组件挂载：

- import `MorningPaperPage` 组件与 `useMorningPaper` hook
- `const morningPaper = useMorningPaper();`
- 收到 WS 消息时调用 `morningPaper.handleWsMessage(msg)`（跟日记页、信件页
  等其它"独立数据页"用同一套接法，各自的 hook 只认自己的消息 type）
- `onRequestMorningPaper` 回调调用 `morningPaper.requestDate(send, forDate)`
  发出 `get_morning_paper` 请求
- `screen === "morning_paper"` 时渲染 `<MorningPaperPage date={...} items={...}
  hasDraft={...} error={...} availableDates={...} feedback={...} loading={...}
  onRequestDate={onRequestMorningPaper} />`（props 直接对应 `useMorningPaper`
  暴露的状态）

## `src/components/MessageBubble.tsx`

聊天气泡渲染正文时，把晨报注入的大段文本折叠成一张卡片而不是直接摊开：

- import `MorningPaperFoldCard` 与 `splitMorningPaperSegments`
- `MessageBubbleProps` 加一个可选回调 `onOpenMorningPaper?: () => void`
- 渲染正文前用 `splitMorningPaperSegments(text)` 切出 `type ===
  "morning_paper"` 的分段，替换成 `<MorningPaperFoldCard text={...}
  count={...} onOpenMorningPaper={onOpenMorningPaper} />`（其余分段照常渲染
  成普通文本）

## `src/components/ChatArea.tsx`

只是把 `onOpenMorningPaper` 这个回调从上层一路透传给 `MessageBubble`（`ChatArea`
本身不含晨报业务逻辑），点击折叠卡片就能跳转到晨报页。

## `src/hooks/useDiary.ts`

日记页 hook 的实现是照 `useMorningPaper.ts` 抄的同一个模式写的（请求走
`get_diary`/回包 `diary`），源码注释里点名参照对象，跟晨报本身没有代码
耦合，只是提一下这个设计模式被复用了。
