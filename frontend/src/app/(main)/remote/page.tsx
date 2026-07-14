import { redirect } from "next/navigation";

export default function RemotePage() {
  redirect("/settings?tab=general");
}
