import type { LucideIcon } from "lucide-react";
import {
  Command as CommandIcon,
  Moon,
  PanelLeft,
  RefreshCw,
  Sun,
} from "lucide-react";
import type { ReactNode } from "react";

import type { Snapshot } from "../../types";
import type { TabKey } from "../../lib/uiTypes";
import type { Theme } from "../../lib/useTheme";
import { Button } from "../ui/button";
import { Badge } from "../ui/badge";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarRail,
  SidebarSeparator,
  SidebarTrigger,
  useSidebar,
} from "../ui/sidebar";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "../ui/tooltip";

export type ShellNavItem = {
  key: TabKey;
  label: string;
  icon: LucideIcon;
};

export function AppShell({
  baseUrl,
  children,
  error,
  loading,
  navItems,
  onCommand,
  onNavigate,
  onRefresh,
  onToggleTheme,
  snapshot,
  tab,
  theme,
  unseenCount,
}: {
  baseUrl: string;
  children: ReactNode;
  error: string | null;
  loading: boolean;
  navItems: ShellNavItem[];
  onCommand: () => void;
  onNavigate: (key: TabKey) => void;
  onRefresh: () => void;
  onToggleTheme: () => void;
  snapshot: Snapshot | null;
  tab: TabKey;
  theme: Theme;
  unseenCount: number;
}) {
  return (
    <TooltipProvider delayDuration={150}>
      <SidebarProvider>
        <Sidebar
          collapsible="icon"
          variant="sidebar"
          className="alfred-glass-shell border-sidebar-border/70"
        >
          <div className="hidden h-3 shrink-0 md:block" data-tauri-drag-region />
          <SidebarHeader className="gap-3 px-3 py-3">
            <SidebarBrandButton onNavigate={onNavigate} />
          </SidebarHeader>

          <SidebarContent>
            <SidebarGroup>
              <SidebarMenu>
                {navItems.map((item) => {
                  const active = item.key === "fleet" ? tab === "fleet" : tab === item.key;
                  const badge =
                    item.key === "fleet" && unseenCount > 0
                      ? unseenCount > 9
                        ? "9+"
                        : String(unseenCount)
                      : null;
                  return (
                    <ShellNavMenuItem
                      key={item.key}
                      active={active}
                      badge={badge}
                      item={item}
                      onNavigate={onNavigate}
                      unseenCount={unseenCount}
                    />
                  );
                })}
              </SidebarMenu>
            </SidebarGroup>
          </SidebarContent>

          <SidebarFooter className="gap-3 border-t border-sidebar-border/50 p-3">
            <div className="group-data-[collapsible=icon]:hidden rounded-lg border border-sidebar-border/55 bg-sidebar-accent/20 p-2">
              <FleetStatus snapshot={snapshot} error={error} />
              <p className="mt-1 truncate text-[11px] text-sidebar-foreground/55" title={baseUrl}>
                {baseUrl.replace(/^https?:\/\//, "")}
              </p>
            </div>
            <SidebarSeparator />
            <div className="grid grid-cols-3 gap-1 group-data-[collapsible=icon]:grid-cols-1">
              <ShellIconButton
                label={error ? "Reconnect" : "Refresh"}
                onClick={onRefresh}
                disabled={loading}
              >
                <RefreshCw
                  aria-hidden="true"
                  className={loading ? "animate-spin" : undefined}
                />
              </ShellIconButton>
              <ShellIconButton label="Commands" onClick={onCommand}>
                <CommandIcon aria-hidden="true" />
              </ShellIconButton>
              <ShellIconButton
                label={theme === "dark" ? "Light theme" : "Dark theme"}
                onClick={onToggleTheme}
              >
                {theme === "dark" ? (
                  <Sun aria-hidden="true" />
                ) : (
                  <Moon aria-hidden="true" />
                )}
              </ShellIconButton>
            </div>
          </SidebarFooter>
          <SidebarRail />
        </Sidebar>

        <SidebarInset className="h-svh overflow-hidden bg-transparent">
          <div className="flex h-full min-w-0 flex-col">
            <header className="alfred-glass flex h-12 shrink-0 items-center gap-2 rounded-none border-x-0 border-t-0 px-3 md:hidden">
              <SidebarTrigger>
                <PanelLeft aria-hidden="true" />
              </SidebarTrigger>
              <span className="alfred-brand-mark size-7 shrink-0" aria-hidden="true">
                <img
                  src="/brand/alfred-logo-transparent.png"
                  alt=""
                  className="alfred-brand-logo size-7 object-contain"
                />
              </span>
              <span className="font-heading text-sm font-medium">Alfred</span>
              <div className="ml-auto">
                <FleetStatus snapshot={snapshot} error={error} compact />
              </div>
            </header>
            <div className="min-h-0 flex-1 overflow-auto px-4 py-4 sm:px-5 lg:px-7">
              {children}
            </div>
          </div>
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
  );
}

function SidebarBrandButton({ onNavigate }: { onNavigate: (key: TabKey) => void }) {
  const { isMobile, setOpenMobile } = useSidebar();
  const navigateHome = () => {
    onNavigate("home");
    if (isMobile) setOpenMobile(false);
  };

  return (
    <button
      className="group-data-[collapsible=icon]:justify-center flex h-12 min-w-0 items-center gap-3 rounded-lg px-2 text-left transition hover:bg-sidebar-accent/45 hover:text-sidebar-accent-foreground"
      type="button"
      onClick={navigateHome}
      aria-label="Open Alfred inbox"
    >
      <span className="alfred-brand-mark size-9 shrink-0" aria-hidden="true">
        <img
          src="/brand/alfred-logo-transparent.png"
          alt=""
          className="alfred-brand-logo size-9 object-contain"
        />
      </span>
      <span className="min-w-0 group-data-[collapsible=icon]:hidden">
        <span className="block truncate font-heading text-base font-semibold">
          Alfred
        </span>
        <span className="block text-[10px] font-medium uppercase tracking-[0.1em] text-sidebar-foreground/55">
          Autonomous coding agents
        </span>
      </span>
    </button>
  );
}

function ShellNavMenuItem({
  active,
  badge,
  item,
  onNavigate,
  unseenCount,
}: {
  active: boolean;
  badge: string | null;
  item: ShellNavItem;
  onNavigate: (key: TabKey) => void;
  unseenCount: number;
}) {
  const { isMobile, setOpenMobile } = useSidebar();
  const Icon = item.icon;
  const navigate = () => {
    onNavigate(item.key);
    if (isMobile) setOpenMobile(false);
  };

  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        isActive={active}
        tooltip={item.label}
        onClick={navigate}
        className="transition-transform duration-150 hover:translate-x-0.5 data-active:translate-x-0.5"
      >
        <Icon aria-hidden="true" />
        <span>{item.label}</span>
      </SidebarMenuButton>
      {badge ? (
        <SidebarMenuBadge aria-label={`${unseenCount} unread`}>
          {badge}
        </SidebarMenuBadge>
      ) : null}
    </SidebarMenuItem>
  );
}

function ShellIconButton({
  children,
  disabled,
  label,
  onClick,
}: {
  children: ReactNode;
  disabled?: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          type="button"
          size="icon-sm"
          variant="ghost"
          disabled={disabled}
          onClick={onClick}
          aria-label={label}
          className="size-8"
        >
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top">{label}</TooltipContent>
    </Tooltip>
  );
}

function FleetStatus({
  compact,
  error,
  snapshot,
}: {
  compact?: boolean;
  error: string | null;
  snapshot: Snapshot | null;
}) {
  const offline = Boolean(error);
  const health = snapshot?.status.reliability.status || "checking";
  const text =
    offline
      ? "Offline"
      : health === "ok"
        ? "Live"
        : health === "checking"
          ? "Checking"
          : "Needs attention";
  const title =
    offline
      ? "Alfred serve offline"
      : health === "ok"
        ? "Agents live"
        : health === "checking"
          ? "Checking agent status"
          : "Agents need attention";
  const variant = offline ? "destructive" : health === "ok" ? "secondary" : "outline";
  const dot =
    offline
      ? "bg-destructive"
      : health === "ok"
        ? "bg-primary"
        : health === "checking"
          ? "bg-muted-foreground"
          : "bg-[var(--warn)]";
  return (
    <Badge
      variant={variant}
      className={compact ? "h-6 gap-1.5 px-2" : "h-7 gap-1.5 px-2"}
      title={title}
    >
      <span
        className={`size-1.5 rounded-full ${dot}`}
        aria-hidden="true"
      />
      {text}
    </Badge>
  );
}
