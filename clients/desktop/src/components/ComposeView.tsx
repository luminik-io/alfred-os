// The Ask surface. Its implementation now lives under `./ask`, rebuilt on
// assistant-ui's ExternalStoreRuntime (we own the chatHistory state; assistant-
// ui renders it). This module re-exports ComposeView so App.tsx and the existing
// test suite keep importing it from the same path.
export { ComposeView } from "./ask/AskThread";
