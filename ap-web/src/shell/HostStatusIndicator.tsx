import { useCallback, useEffect, useRef, useState } from "react";
import { PlayIcon, RotateCwIcon, SquareIcon } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  controlHost,
  controlServer,
  getHostStatus,
  getLocalServerStatus,
  isElectronShell,
  onHostStatusChanged,
  type HostControlAction,
  type HostStatus,
  type LocalServerStatus,
} from "@/lib/nativeBridge";
import { cn } from "@/lib/utils";

/** Dot color from host status. */
function hostTone(status: HostStatus): string {
  if (!status.cliInstalled) return "bg-muted-foreground/40";
  if (status.connected) return "bg-success";
  if (status.process === "online") return "bg-warning";
  return "bg-muted-foreground/40";
}

/** Status summary shown atop the host menu. */
function hostText(status: HostStatus): string {
  if (!status.cliInstalled) return "Omnigent CLI not found";
  if (status.error) return status.error;
  if (status.connected) {
    return status.sessions > 0
      ? `Connected · ${status.sessions} session${status.sessions === 1 ? "" : "s"}`
      : "Connected";
  }
  if (status.process === "online") return "Connecting…";
  return "Not hosting";
}

/** Status summary shown atop the local-server menu. */
function serverText(server: LocalServerStatus): string {
  if (!server.running) return "Stopped";
  return server.liveSessions > 0 ? `Running · ${server.liveSessions} active` : "Running";
}

/**
 * A sidebar status row that opens a Start / Stop / Restart menu. The trigger
 * shows a title and a status dot; the menu's items enable/disable by whether
 * the thing is currently active, and everything is disabled while an action is
 * in flight or control isn't possible (CLI missing).
 */
function StatusMenu({
  title,
  tone,
  statusText,
  active,
  canControl,
  busy,
  onAction,
}: {
  title: string;
  tone: string;
  statusText: string;
  active: boolean;
  canControl: boolean;
  busy: boolean;
  onAction: (action: HostControlAction) => void;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className={cn(
          "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-sm",
          "text-muted-foreground hover:bg-foreground/5 hover:text-foreground",
          "data-[state=open]:bg-foreground/5 data-[state=open]:text-foreground",
        )}
      >
        <span className="truncate">{title}</span>
        <span aria-hidden className={cn("ml-auto size-2 shrink-0 rounded-full", tone)} />
      </DropdownMenuTrigger>
      <DropdownMenuContent side="top" align="start" className="min-w-48">
        <DropdownMenuLabel className="text-xs font-normal text-muted-foreground">
          {statusText}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="gap-2"
          disabled={busy || !canControl || active}
          onSelect={() => onAction("start")}
        >
          <PlayIcon className="size-4 shrink-0" />
          Start
        </DropdownMenuItem>
        <DropdownMenuItem
          className="gap-2"
          disabled={busy || !canControl || !active}
          onSelect={() => onAction("stop")}
        >
          <SquareIcon className="size-4 shrink-0" />
          Stop
        </DropdownMenuItem>
        <DropdownMenuItem
          className="gap-2"
          disabled={busy || !canControl || !active}
          onSelect={() => onAction("restart")}
        >
          <RotateCwIcon className="size-4 shrink-0" />
          Restart
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/**
 * Desktop-shell host controls in the sidebar footer next to Settings.
 *
 * A "Host Status" row (this machine's host daemon) and — when connected to a
 * local server — a "Local Server Status" row. Each shows a status dot and opens
 * a Start / Stop / Restart menu driving the omnigent CLI through the shell.
 * Status is read live (`getHostStatus` + the shell's pushed updates); hosting
 * can also be opted into at connect time on the setup page.
 *
 * Renders nothing outside the Electron shell or before the shell reports a
 * status. Desktop-only by layout (hidden on the narrow mobile sidebar).
 */
export function HostStatusIndicator() {
  const [host, setHost] = useState<HostStatus | null>(null);
  const [server, setServer] = useState<LocalServerStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(() => {
    void getHostStatus().then((s) => {
      if (mounted.current) setHost(s);
    });
    void getLocalServerStatus().then((s) => {
      if (mounted.current) setServer(s);
    });
  }, []);

  useEffect(() => {
    if (!isElectronShell()) return;
    refresh();
    return onHostStatusChanged(() => refresh());
  }, [refresh]);

  if (!host) return null;

  const canControl = host.cliInstalled;
  const hostActive = host.connected || host.process === "online";

  const run =
    (control: (action: HostControlAction) => Promise<unknown>) =>
    async (action: HostControlAction) => {
      setBusy(true);
      try {
        await control(action);
      } finally {
        refresh();
        if (mounted.current) setBusy(false);
      }
    };

  return (
    <div className="max-md:hidden">
      <StatusMenu
        title="Host Status"
        tone={hostTone(host)}
        statusText={hostText(host)}
        active={hostActive}
        canControl={canControl}
        busy={busy}
        onAction={run(controlHost)}
      />
      {server && (
        <StatusMenu
          title="Local Server Status"
          tone={server.running ? "bg-success" : "bg-muted-foreground/40"}
          statusText={serverText(server)}
          active={server.running}
          canControl={canControl}
          busy={busy}
          onAction={run(controlServer)}
        />
      )}
    </div>
  );
}
