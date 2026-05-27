// Profile editor route — /config/profiles/$profileName (ALP-682).
// Inherits the protected AuthedLayout (Nav + AlertBanner) from _authed.tsx.
import { createRoute } from '@tanstack/react-router'

import { Route as AuthedRoute } from '@/routes/_authed'
import { ProfileEditorPage } from '@/views/configuration/profiles/profile-editor-page'

export const Route = createRoute({
  getParentRoute: () => AuthedRoute,
  path: '/config/profiles/$profileName',
  component: function ProfileNamePage() {
    const { profileName } = Route.useParams()
    return <ProfileEditorPage profileName={profileName} />
  },
})
