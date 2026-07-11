export type BackendPhase = "initializing" | "ready" | "restarting" | "failed";

/** Snapshot owned and emitted by the native Tauri backend manager. */
export interface BackendStatus {
  phase: BackendPhase;
  revision: number;
  url?: string | null;
  attempt?: number | null;
  max_attempts?: number | null;
  failure_code?: string | null;
  detail?: string | null;
}

export interface BackendLifecycleState {
  status: BackendStatus;
  relaunching: boolean;
  actionError: string | null;
}

export type BackendLifecycleAction =
  | { type: "native-status"; status: BackendStatus }
  | { type: "bootstrap-failed"; detail: string }
  | { type: "relaunch-requested" }
  | { type: "action-failed"; detail: string }
  | { type: "action-cleared" };

export const INITIAL_DESKTOP_BACKEND_STATE: BackendLifecycleState = {
  status: { phase: "initializing", revision: 0 },
  relaunching: false,
  actionError: null,
};

export const READY_WEB_BACKEND_STATE: BackendLifecycleState = {
  status: { phase: "ready", revision: 0 },
  relaunching: false,
  actionError: null,
};

export function backendLifecycleReducer(
  state: BackendLifecycleState,
  action: BackendLifecycleAction,
): BackendLifecycleState {
  switch (action.type) {
    case "native-status": {
      // The listener is installed before the initial snapshot is requested.
      // A newer event can therefore arrive before an older invoke response.
      if (action.status.revision <= state.status.revision) return state;
      return {
        status: action.status,
        relaunching: false,
        actionError: null,
      };
    }
    case "bootstrap-failed":
      // If a native event already arrived, a later snapshot invoke failure
      // must not replace that authoritative state.
      if (state.status.revision > 0) return state;
      return {
        status: {
          phase: "failed",
          revision: state.status.revision,
          failure_code: "lifecycle_unavailable",
          detail: action.detail,
        },
        relaunching: false,
        actionError: null,
      };
    case "relaunch-requested":
      if (state.status.phase !== "failed") return state;
      return { ...state, relaunching: true, actionError: null };
    case "action-failed":
      return { ...state, relaunching: false, actionError: action.detail };
    case "action-cleared":
      return { ...state, actionError: null };
  }
}
