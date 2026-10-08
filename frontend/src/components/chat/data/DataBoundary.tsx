"use client";

import { Component, type ReactNode } from "react";

/**
 * If a reply's tables or charts ever fail to render, drop them and keep the conversation:
 * the reply text is rendered outside this boundary and stays visible.
 */
export class DataBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}
