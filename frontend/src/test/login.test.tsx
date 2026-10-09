import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

const navigate = vi.fn();
vi.mock("@tanstack/react-router", async (orig) => ({
  ...(await orig<typeof import("@tanstack/react-router")>()),
  useNavigate: () => navigate,
}));

import { Login } from "@/routes/index";
import { session } from "@/services";

describe("Login page", () => {
  it("shows an error with wrong credentials", async () => {
    render(<Login />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "admin@scuola.it" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "bad" } });
    fireEvent.click(screen.getByRole("button", { name: "Entra" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Email o password non corretti");
  });

  it("stores the session and navigates on success", async () => {
    render(<Login />);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "segreteria@scuola.it" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "password123" } });
    fireEvent.click(screen.getByRole("button", { name: "Entra" }));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/menu" }));
    expect(session.getRole()).toBe("SEGRETERIA");
  });
});
