"use client";
import { createContext, useContext } from "react";

export const WorkspaceAccess = createContext(false);
export function useCanManageWorkspace() {
    return useContext(WorkspaceAccess);
}
