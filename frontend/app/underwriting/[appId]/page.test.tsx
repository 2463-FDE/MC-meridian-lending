// Regression tests for two ways the underwriting detail screen could show one
// applicant's regulated decision facts in the wrong place (PR review):
//
//  1. A manual "Run decision" left the previous assistant card on screen, so a
//     superseded outcome sat beside the new primary decision.
//  2. This page component is reused across /underwriting/[appId] navigations, so an
//     assistant call started on one application could still be in flight when the
//     officer opened another, and its response stamped the earlier applicant's facts
//     onto the new screen.
//
// Both assert on what the officer actually sees, not on internal state.

import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import UnderwritingDetailPage from "./page";

// The live route param. The mock reads it on every call, so assigning to it and
// re-rendering models a client-side navigation to another application.
let routeAppId = "1";

vi.mock("next/navigation", () => ({
  useParams: () => ({ appId: routeAppId }),
}));

vi.mock("next/link", () => ({
  default: ({ children }: { children?: ReactNode }) => <a>{children}</a>,
}));

const apiGet = vi.fn();
const apiPost = vi.fn();

// The factory bodies stay lazy so they resolve apiGet/apiPost at call time, after the
// consts above are initialised (vi.mock is hoisted above them).
vi.mock("../../../lib/api", () => ({
  apiGet: (...args: unknown[]) => apiGet(...args),
  apiPost: (...args: unknown[]) => apiPost(...args),
}));

const APP_1 = {
  id: 1,
  applicant: { name: "Maria Alvarez" },
  amount: 12000,
  term_months: 48,
  purpose: "debt_consolidation",
  status: "submitted",
};

const APP_2 = {
  ...APP_1,
  id: 2,
  applicant: { name: "Dan Brown" },
};

const ASSISTANT_SUMMARY = "Approved on a thin but clean file.";

function assistantResultFor(applicationId: number) {
  return {
    application_id: applicationId,
    outcome: "approved",
    score: 712,
    policy_band: "prime",
    principal_reasons: [{ code: "R1", reason: "Limited credit history" }],
    decided_by: "assistant",
    decided_at: "2026-07-01T00:00:00Z",
    summary: ASSISTANT_SUMMARY,
    narration_validated: true,
  };
}

beforeEach(() => {
  routeAppId = "1";
  apiGet.mockReset();
  apiPost.mockReset();
  apiGet.mockImplementation(async (path: string) => {
    if (path === "/los/applications/1") return APP_1;
    if (path === "/los/applications/2") return APP_2;
    throw new Error(`unexpected GET ${path}`);
  });
});

afterEach(() => {
  cleanup();
});

describe("underwriting detail — assistant panel", () => {
  it("clears the assistant card once a manual decision supersedes it", async () => {
    apiGet.mockImplementation(async (path: string) => {
      if (path === "/los/applications/1") return APP_1;
      if (path === "/los/assistant/decisions/1") return assistantResultFor(1);
      throw new Error(`unexpected GET ${path}`);
    });
    apiPost.mockResolvedValue({ app_id: 1, decision: "declined", score: 640 });

    render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");

    // Explain populates the assistant card from the recorded decision.
    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByText(ASSISTANT_SUMMARY)).toBeTruthy();

    // A manual decision records a NEW event, so the card now describes a superseded one.
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    await screen.findByText("Decision recorded: declined.");

    expect(screen.queryByText(ASSISTANT_SUMMARY)).toBeNull();
  });

  it("drops an in-flight assistant response after the route moves to another application", async () => {
    let resolveAssistant: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      resolveAssistant = resolve;
    });
    apiPost.mockImplementation((path: string) => {
      if (path === "/los/assistant/decisions/1") return pending;
      throw new Error(`unexpected POST ${path}`);
    });

    const view = render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");

    // Start the assistant run on application 1 and leave it unresolved.
    fireEvent.click(screen.getByRole("button", { name: "Run AI assistant" }));

    // Navigate to application 2 while that call is still in flight.
    routeAppId = "2";
    view.rerender(<UnderwritingDetailPage />);
    await screen.findAllByText("Dan Brown");

    // Application 1's response lands on application 2's screen.
    await act(async () => {
      resolveAssistant(assistantResultFor(1));
    });

    expect(screen.queryByText(ASSISTANT_SUMMARY)).toBeNull();
    expect(screen.queryByText(/Limited credit history/)).toBeNull();
  });

  it("hides the previous applicant while the next application is still loading", async () => {
    let resolveApp2: (value: unknown) => void = () => {};
    const pendingApp2 = new Promise((resolve) => {
      resolveApp2 = resolve;
    });
    apiGet.mockImplementation((path: string) => {
      // Application 1 carries a recorded decision, the regulated fact that must not
      // survive the navigation. Application 2's load never settles.
      if (path === "/los/applications/1") {
        return Promise.resolve({ ...APP_1, decision: "declined" });
      }
      if (path === "/los/applications/2") return pendingApp2;
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    const view = render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");
    expect(screen.getByText("Declined")).toBeTruthy();

    // Navigate to application 2 and hold its load open.
    routeAppId = "2";
    view.rerender(<UnderwritingDetailPage />);

    // Application 1's applicant and decision must not sit under application 2's header.
    expect(screen.queryAllByText("Maria Alvarez")).toHaveLength(0);
    expect(screen.queryByText("Declined")).toBeNull();
    expect(screen.getByText(/Loading application #2/)).toBeTruthy();

    await act(async () => {
      resolveApp2(APP_2);
    });
    await screen.findAllByText("Dan Brown");
  });

  it("drops an in-flight assistant response after navigating away and back", async () => {
    let resolveAssistant: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      resolveAssistant = resolve;
    });
    apiPost.mockImplementation((path: string) => {
      if (path === "/los/assistant/decisions/1") return pending;
      throw new Error(`unexpected POST ${path}`);
    });

    const view = render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");
    fireEvent.click(screen.getByRole("button", { name: "Run AI assistant" }));

    // Away to application 2, then straight back to 1, still in flight. The response now
    // carries the id of the application on screen, so an id comparison would accept it;
    // it still predates the navigation that cleared the panel.
    routeAppId = "2";
    view.rerender(<UnderwritingDetailPage />);
    await screen.findAllByText("Dan Brown");
    routeAppId = "1";
    view.rerender(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");

    await act(async () => {
      resolveAssistant(assistantResultFor(1));
    });

    expect(screen.queryByText(ASSISTANT_SUMMARY)).toBeNull();
  });
});
