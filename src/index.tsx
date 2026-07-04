import { definePlugin, staticClasses } from '@decky/ui'
import { call, routerHook } from '@decky/api'
import { GiMusicalNotes } from 'react-icons/gi'

import SettingsPanel from './components/settings'
import patchContextMenu, { LibraryContextMenu } from './lib/patchContextMenu'
import ChangeTheme from './components/changeTheme'
import {
  AudioLoaderCompatState,
  AudioLoaderCompatStateContextProvider
} from './state/AudioLoaderCompatState'
import { setCurrentLibraryAppId } from './state/currentApp'
import { getCache } from './cache/musicCache'
import { getResolver } from './actions/audio'
import { Settings, defaultSettings } from './hooks/useSettings'

type AppState = {
  unAppID: number
  bRunning: boolean
}

// Polls window.location.pathname — Steam updates this URL on every navigation,
// so we can reliably detect game page changes without needing Decky's broken router patch.
function createAudioController() {
  let audio: HTMLAudioElement | null = null
  let currentAppid: string | null = null
  let currentPath = ''
  // Monotonic token to cancel stale async loads when the user navigates away mid-load
  let inflightToken = 0
  let intervalId: ReturnType<typeof setInterval> | null = null

  function stopAudio() {
    if (audio) {
      audio.pause()
      audio.src = ''
      audio = null
    }
  }

  async function loadAndPlay(appid: string, token: number) {
    try {
      const settings = await call<[string, Settings], Settings>(
        'get_setting',
        'settings',
        defaultSettings
      )
      if (inflightToken !== token) return
      if (settings.defaultMuted) return

      const appidNum = parseInt(appid)
      const cache = await getCache(appidNum)
      if (inflightToken !== token) return

      if (!cache?.videoId) return
      if (cache.videoId.length === 0) return

      const resolver = getResolver(settings.useYtDlp)
      const audioUrl = (await resolver.getAudioUrlFromVideo({ id: cache.videoId })) || ''
      if (inflightToken !== token || !audioUrl) return

      const newAudio = new Audio(audioUrl)
      newAudio.loop = true
      newAudio.preload = 'auto'
      newAudio.volume =
        typeof cache?.volume === 'number' ? cache.volume : settings.volume

      if (inflightToken !== token) return
      audio = newAudio

      await new Promise<void>((resolve, reject) => {
        newAudio.oncanplaythrough = () => resolve()
        newAudio.onerror = () => {
          const err = newAudio.error
          reject(new Error(`audio load error code=${err?.code} msg=${err?.message}`))
        }
        newAudio.load()
      })

      if (inflightToken !== token) return
      newAudio.play().catch((e: unknown) =>
        console.error('[GameThemeMusic] play error:', e)
      )
    } catch (e) {
      console.error('[GameThemeMusic] loadAndPlay error:', e)
    }
  }

  function tick() {
    const path = window.location.pathname
    if (path === currentPath) return
    currentPath = path

    const match = path.match(/\/routes\/library\/app\/(\d+)/)
    const appid = match ? match[1] : null

    setCurrentLibraryAppId(appid)

    if (appid === currentAppid) return
    currentAppid = appid
    inflightToken++
    stopAudio()

    if (appid) {
      loadAndPlay(appid, inflightToken)
    }
  }

  return {
    start() {
      intervalId = setInterval(tick, 300)
      tick() // immediately handle whatever page we're already on
    },
    stop() {
      if (intervalId !== null) {
        clearInterval(intervalId)
        intervalId = null
      }
      inflightToken++
      stopAudio()
      setCurrentLibraryAppId(null)
      currentAppid = null
      currentPath = ''
    }
  }
}

export default definePlugin(() => {
  const state: AudioLoaderCompatState = new AudioLoaderCompatState()
  const audioController = createAudioController()
  audioController.start()

  routerHook.addRoute(
    '/gamethememusic/:appid',
    () => (
      <AudioLoaderCompatStateContextProvider AudioLoaderCompatStateClass={state}>
        <ChangeTheme />
      </AudioLoaderCompatStateContextProvider>
    ),
    { exact: true }
  )

  const patchedMenu = LibraryContextMenu
    ? patchContextMenu(LibraryContextMenu)
    : null

  const AppStateRegistrar =
    SteamClient.GameSessions.RegisterForAppLifetimeNotifications(
      (update: AppState) => {
        const { gamesRunning } = state.getPublicState()
        const setGamesRunning = state.setGamesRunning.bind(state)
        if (update.bRunning) {
          setGamesRunning([...gamesRunning, update.unAppID])
        } else {
          setGamesRunning(gamesRunning.filter((e: number) => e !== update.unAppID))
        }
      }
    )

  return {
    title: <div className={staticClasses.Title}>Game Theme Music</div>,
    icon: <GiMusicalNotes />,
    content: <SettingsPanel />,
    onDismount() {
      AppStateRegistrar.unregister()
      audioController.stop()
      routerHook.removeRoute('/gamethememusic/:appid')
      patchedMenu?.unpatch()
    }
  }
})
