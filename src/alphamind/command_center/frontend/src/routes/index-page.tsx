// Index landing page — a placeholder under the protected layout. View
// stories overwrite this with the default landing view (05b's Live run
// watcher is the likely target).

export function IndexPage(): React.JSX.Element {
  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold">Welcome</h1>
      <p className="text-muted-foreground">
        Per-view dashboards land here as stories 05b–05j and 06a–06c ship.
      </p>
    </div>
  )
}
