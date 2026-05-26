import { createRoute } from '@tanstack/react-router'

import { InvocationDetailPage } from '@/views/history/invocation-detail/invocation-detail-page'

import { Route as AuthedRoute } from '../../_authed'

// /history/$invocationId — per-invocation detail page (story 05d / ALP-674).
// Component is defined inline to satisfy react-refresh (route file exports only Route).
export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/history/$invocationId',
  component: function InvocationDetailRoute(): React.JSX.Element {
    const { invocationId } = Route.useParams()
    return <InvocationDetailPage invocationId={invocationId} />
  },
})
