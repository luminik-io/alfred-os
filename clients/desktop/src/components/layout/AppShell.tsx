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
          variant="inset"
          className="border-sidebar-border/70 bg-sidebar/80 backdrop-blur-2xl"
        >
          <SidebarHeader className="gap-3 p-3">
            <button
              className="group-data-[collapsible=icon]:justify-center flex h-11 min-w-0 items-center gap-3 rounded-lg px-2 text-left transition hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
              type="button"
              onClick={() => onNavigate("review")}
              aria-label="Alfred home"
            >
              <span className="alfred-brand-mark size-9 shrink-0">
                <img
                  src="/brand/alfred-logo-transparent.png"
                  alt=""
                  className="relative z-10 size-8 object-contain drop-shadow-[0_8px_18px_rgba(0,0,0,0.28)]"
                />
              </span>
              <span className="min-w-0 group-data-[collapsible=icon]:hidden">
                <span className="block truncate font-heading text-sm font-medium">
                  Alfred
                </span>
                <span className="block truncate text-xs text-sidebar-foreground/65">
                  Agent console
                </span>
              </span>
            </button>
          </SidebarHeader>

          <SidebarContent>
            <SidebarGroup>
              <SidebarMenu>
                {navItems.map((item) => {
                  const Icon = item.icon;
                  const active = tab === item.key;
                  const badge =
                    item.key === "operator" && unseenCount > 0
                      ? unseenCount > 9
                        ? "9+"
                        : String(unseenCount)
                      : null;
                  return (
                    <SidebarMenuItem key={item.key}>
                      <SidebarMenuButton
                        isActive={active}
                        tooltip={item.label}
                        onClick={() => onNavigate(item.key)}
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
                })}
              </SidebarMenu>
            </SidebarGroup>
          </SidebarContent>

          <SidebarFooter className="gap-3 p-3">
            <div className="group-data-[collapsible=icon]:hidden rounded-lg border border-sidebar-border/70 bg-sidebar-accent/35 p-2">
              <FleetStatus snapshot={snapshot} error={error} />
              <p className="mt-1 truncate text-[11px] text-sidebar-foreground/55" title={baseUrl}>
                {baseUrl}
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

        <SidebarInset className="overflow-hidden bg-transparent">
          <div className="flex h-svh min-w-0 flex-col">
            <header className="alfred-glass flex h-12 shrink-0 items-center gap-2 rounded-none border-x-0 border-t-0 px-3 md:hidden">
              <SidebarTrigger>
                <PanelLeft aria-hidden="true" />
              </SidebarTrigger>
              <span className="alfred-brand-mark size-7 shrink-0">
                <img
                  src="/brand/alfred-logo-transparent.png"
                  alt=""
                  className="relative z-10 size-6 object-contain"
                />
              </span>
              <span className="font-heading text-sm font-medium">Alfred</span>
              <div className="ml-auto">
                <FleetStatus snapshot={snapshot} error={error} compact />
              </div>
            </header>
            <div className="min-h-0 flex-1 overflow-auto px-4 py-4 sm:px-5 lg:px-6">
              {children}
            </div>
          </div>
        </SidebarInset>
      </SidebarProvider>
    </TooltipProvider>
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
  const text = offline ? "Offline" : health === "ok" ? "Live" : titleCase(health);
  const variant = offline ? "destructive" : health === "ok" ? "secondary" : "outline";
  return (
    <Badge
      variant={variant}
      className={compact ? "h-6 gap-1.5 px-2" : "h-7 gap-1.5 px-2"}
      title={offline ? "Alfred serve offline" : `Agents ${text.toLowerCase()}`}
    >
      <span
        className={
          offline
            ? "size-1.5 rounded-full bg-destructive"
            : "size-1.5 rounded-full bg-emerald-500"
        }
        aria-hidden="true"
      />
      {text}
    </Badge>
  );
}

function titleCase(value: string): string {
  return value
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((part) => part[0]?.toUpperCase() + part.slice(1))
    .join(" ");
}
