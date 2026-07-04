let _currentLibraryAppId: string | null = null
export const currentAppBus = new EventTarget()

export const getCurrentLibraryAppId = () => _currentLibraryAppId
export const setCurrentLibraryAppId = (id: string | null) => {
  if (id === _currentLibraryAppId) return
  _currentLibraryAppId = id
  currentAppBus.dispatchEvent(new Event('change'))
}
