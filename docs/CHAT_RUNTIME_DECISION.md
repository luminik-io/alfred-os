# Chat Runtime Decision

Date: 2026-06-07

## Decision

Alfred should use a layered chat architecture:

1. **Chat SDK by Vercel** for multi-messenger transport.
   Use it when adding Slack, Teams, Google Chat, Discord, Telegram, WhatsApp,
   GitHub, Linear, or browser/web channels behind one normalized bot runtime.

2. **Vercel AI SDK UI protocol** for streaming messages and tool results.
   This keeps the browser/native client on a common transport shape, even if
   the backend remains Python-first.

3. **assistant-ui** for the native/web React chat surface.
   It gives Alfred shadcn-based chat primitives, runtime adapters, thread
   state, and enough control to keep the native client visually custom.

4. **CopilotKit only for later generative UI needs.**
   It is valuable when agents should render registered React components,
   stream agent state, or compose UI from a guarded catalog. That is powerful,
   but it is not the first dependency Alfred needs.

## Why

Alfred has two different jobs that should not be collapsed:

- Messenger ingress and egress: Slack today, more messengers later.
- AI-native command room UX: native client chat, fleet controls, plans,
  memory, issue routing, and approvals.

Chat SDK is the right fit for the first job. Its official README describes a
single TypeScript bot runtime for Slack, Teams, Google Chat, Discord, Telegram,
GitHub, Linear, and WhatsApp. Its adapter docs show platform adapters,
normalized webhooks, cross-platform cards/actions, and a web adapter that
speaks the AI SDK UI stream protocol.

AI SDK UI and assistant-ui are the right fit for the second job. AI SDK
`useChat` has a transport-based API with tool-call handling, stream resume,
and custom transports. assistant-ui layers polished React/shadcn chat
components and runtimes on top, including Vercel AI SDK support and an
external-store runtime for apps that own their message store.

CopilotKit is compelling for rich generative UI, but adopting it first would
pull Alfred toward AG-UI and agent-rendered components before the product has
settled the core messenger and command-room contract.

## Architecture

```text
Slack / Teams / WhatsApp / Web
        |
        v
Messenger Gateway (Chat SDK adapters, TypeScript)
        |
        v
Alfred Conversation Core
  - route intent
  - load repo/fleet context
  - call Codex / Claude Code / local tools
  - enforce human gates
  - emit typed events and tool results
        |
        +--> Slack or other messenger replies
        |
        +--> Local API for native client
                |
                v
        Tauri React client
        assistant-ui + AI SDK UI transport
```

The conversation core remains the product boundary. Messenger adapters should
not decide agent policy. Native UI should not shell out directly for privileged
actions unless it goes through the same typed command and approval path.

## Migration Order

1. Keep the current Python Slack listener running.
2. Add a typed conversation-command contract around existing Slack controls:
   `status`, `run`, `dry-run`, `pause`, `resume`, `queue`, `hold`, `plans`,
   `memory`, and trusted-user mutations.
3. Add a local `/api/chat` or `/api/conversation` endpoint that streams AI SDK
   UI-compatible message chunks for the native client.
4. Rebuild the native chat pane with assistant-ui over that local endpoint.
5. Add a TypeScript Chat SDK sidecar for new messenger adapters. Start with
   Slack in shadow mode or low-risk slash commands, then move full Slack
   ingress once parity is proven.
6. Add Teams or WhatsApp through Chat SDK after Slack parity is tested.
7. Evaluate CopilotKit for plan cards, approval cards, and fleet-state
   generative UI only after the typed command contract is stable.

## Non-Goals

- Do not rewrite Alfred's core policy layer into a messenger framework.
- Do not let model output invoke privileged actions directly. Actions stay
  typed, audited, and gated.
- Do not make native client chat a separate assistant from Slack chat. Both
  surfaces should talk to the same conversation core.
- Do not adopt CopilotKit just for chat chrome. assistant-ui is the cleaner
  first step for chat UX.

## Source Notes

- Chat SDK: https://github.com/vercel/chat
- Chat SDK adapters: https://chat-sdk.dev/docs/adapters
- Chat SDK Slack primitives: https://chat-sdk.dev/docs/slack-primitives
- Chat SDK AI tools changelog: https://vercel.com/changelog/chat-sdk-now-includes-ai-sdk-tools
- AI SDK `useChat`: https://ai-sdk.dev/docs/reference/ai-sdk-ui/use-chat
- AI SDK transport: https://ai-sdk.dev/docs/ai-sdk-ui/transport
- assistant-ui architecture: https://www.assistant-ui.com/docs/architecture
- CopilotKit generative UI: https://docs.copilotkit.ai/concepts/generative-ui-overview
