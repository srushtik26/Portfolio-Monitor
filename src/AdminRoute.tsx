import type { ReactNode } from "react";
import { ShieldAlert } from "lucide-react";
import { useAuth } from "./AuthContext";

export default function AdminRoute({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  if (user?.role === "ADMIN") return children;
  return (
    <div className="empty-state" role="alert">
      <ShieldAlert size={20} />
      <b>Administrator access required</b>
    </div>
  );
}