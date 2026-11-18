import { isAudioAsset } from './assetClip'
import { clipSourceStart, clipSpeed } from './clipPlayback'
import { AUDIO_TYPES, mediaFileName, VISUAL_TYPES, xmlEscape } from './premiereXml'
import type { TimelineAsset, TimelineClip, TimelineProject } from './types'

/**
 * Timeline → FCPXML 1.10 for Final Cut Pro (File > Import > XML). Everything hangs off one
 * gap in the primary storyline as connected clips: visual tracks on lanes 1, 2, … (higher is
 * on top) and sound tracks on lanes -1, -2, …, so every clip keeps its exact timeline time.
 * Media is referenced by file name and relinked in Final Cut; subtitles travel as SRT.
 */

export type FcpXmlResult = {
  xml: string
  clips: number
  warnings: string[]
}

type Resources = { fps: number; assets: Map<string, string>; lines: string[] }

function time(frames: number, fps: number): string {
  return frames === 0 ? '0s' : `${frames}/${fps}s`
}

function assetRef(asset: TimelineAsset, res: Resources): string {
  const known = res.assets.get(asset.id)
  if (known) {
    return known
  }
  const id = `r${res.assets.size + 2}`
  res.assets.set(asset.id, id)
  const name = mediaFileName(asset)
  const still = asset.type === 'image'
  const hasVideo = asset.type === 'video' || still
  const hasAudio = asset.type === 'video' || isAudioAsset(asset.type)
  const frames = still ? 0 : Math.round((asset.duration ?? 0) * res.fps)
  res.lines.push(
    `<asset id="${id}" name="${xmlEscape(name.replace(/\.[^.]+$/, ''))}" start="0s" duration="${time(frames, res.fps)}"` +
      ` hasVideo="${hasVideo ? 1 : 0}" hasAudio="${hasAudio ? 1 : 0}"${hasVideo ? ' format="r1"' : ''}` +
      `${hasAudio ? ' audioSources="1" audioChannels="2"' : ''}>` +
      `<media-rep kind="original-media" src="file:///${encodeURIComponent(name)}"/></asset>`,
  )
  return id
}

function connectedClip(clip: TimelineClip, asset: TimelineAsset, lane: number, res: Resources): string {
  const ref = assetRef(asset, res)
  const offset = time(Math.round(clip.start * res.fps), res.fps)
  const duration = time(Math.round(clip.duration * res.fps), res.fps)
  const name = xmlEscape(clip.label || mediaFileName(asset))
  if (asset.type === 'image') {
    return `<video ref="${ref}" lane="${lane}" offset="${offset}" start="0s" duration="${duration}" name="${name}"/>`
  }
  const start = time(Math.round(clipSourceStart(clip) * res.fps), res.fps)
  return `<asset-clip ref="${ref}" lane="${lane}" offset="${offset}" start="${start}" duration="${duration}" name="${name}"/>`
}

export function buildFcpXml(project: TimelineProject): FcpXmlResult {
  const fps = Math.round(project.meta.fps || 30)
  const res: Resources = { fps, assets: new Map(), lines: [] }
  const assets = new Map(project.assets.map((asset) => [asset.id, asset]))
  const warnings: string[] = []
  const clips: string[] = []

  const onTrack = (trackId: string, types: Set<string>) =>
    project.timeline.clips
      .filter((clip) => clip.trackId === trackId && types.has(clip.type) && clip.asset && assets.has(clip.asset))
      .sort((a, b) => a.start - b.start)

  let visualLane = 0
  let soundLane = 0
  for (const track of project.timeline.tracks) {
    const visual = onTrack(track.id, VISUAL_TYPES)
    if (visual.length) {
      visualLane += 1
      for (const clip of visual) {
        clips.push(connectedClip(clip, assets.get(clip.asset as string) as TimelineAsset, visualLane, res))
      }
    }
    const sound = onTrack(track.id, AUDIO_TYPES).filter((clip) => isAudioAsset((assets.get(clip.asset as string) as TimelineAsset).type))
    if (sound.length) {
      soundLane -= 1
      for (const clip of sound) {
        clips.push(connectedClip(clip, assets.get(clip.asset as string) as TimelineAsset, soundLane, res))
      }
    }
  }

  if (project.timeline.clips.some((clip) => clip.asset && clipSpeed(clip) !== 1)) {
    warnings.push('속도를 바꾼 클립은 원래 속도로 들어갑니다. 파이널컷에서 속도를 다시 맞춰 주세요.')
  }
  if (project.timeline.clips.some((clip) => clip.type === 'subtitle' || clip.type === 'text')) {
    warnings.push('자막은 FCPXML에 들어가지 않습니다. SRT 자막을 함께 가져오세요.')
  }

  const { width, height } = project.meta
  const name = xmlEscape(project.meta.name || 'Flow')
  const total = time(Math.round(project.meta.duration * fps), fps)
  const xml =
    '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n<fcpxml version="1.10"><resources>' +
    `<format id="r1" name="FFVideoFormat${height}p${fps}" frameDuration="1/${fps}s" width="${width}" height="${height}"/>` +
    `${res.lines.join('')}</resources>` +
    `<library><event name="Flow"><project name="${name}">` +
    `<sequence format="r1" duration="${total}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">` +
    `<spine><gap name="Gap" offset="0s" start="0s" duration="${total}">${clips.join('')}</gap></spine>` +
    '</sequence></project></event></library></fcpxml>\n'
  return { xml, clips: clips.length, warnings }
}
