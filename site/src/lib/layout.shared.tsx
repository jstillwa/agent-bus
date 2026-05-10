import type { BaseLayoutProps } from "fumadocs-ui/layouts/shared";
import { GitHubNavAction } from "@/components/github-nav-action";
import { appName, githubProjectUrl } from "@/lib/shared";

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      children: <GitHubNavAction />,
      title: appName,
    },
    githubUrl: githubProjectUrl,
  };
}
