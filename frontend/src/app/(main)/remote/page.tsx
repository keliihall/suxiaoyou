"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

export default function RemotePage() {
  const router = useRouter();

  useEffect(() => {
    router.replace("/settings?tab=general");
  }, [router]);

  return null;
}
