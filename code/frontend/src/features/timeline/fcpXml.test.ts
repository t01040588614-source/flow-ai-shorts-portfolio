import { spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { buildFcpXml } from './fcpXml'
import type { TimelineProject } from './types'

function project(): TimelineProject {
  return {
    version: 1,
    meta: { id: 'p', name: '인터뷰 & 편집', updatedAt: '', duration: 10, fps: 30, width: 1920, height: 1080 },
    timeline: {
      tracks: [
        { id: 'track-video', name: 'Video', kind: 'video' },
        { id: 'track-images', name: 'Images', kind: 'image' },
        { id: 'track-subtitle', name: 'Subtitle', kind: 'subtitle' },
        { id: 'track-music', name: 'Music', kind: 'music' },
      ],
      clips: [
        { id: 'v2', trackId: 'track-video', type: 'video', start: 4, duration: 2, asset: 'vid', sourceStart: 6 },
        { id: 'v1', trackId: 'track-video', type: 'video', start: 0, duration: 4, asset: 'vid' },
        { id: 'img', trackId: 'track-images', type: 'image', start: 1, duration: 2, asset: 'logo' },
        { id: 's', trackId: 'track-subtitle', type: 'subtitle', start: 0, duration: 2, text: '자막' },
        { id: 'm', trackId: 'track-music', type: 'music', start: 0, duration: 6, asset: 'bgm', speed: 1.5 },
      ],
    },
    assets: [
      { id: 'vid', name: 'interview.mp4', type: 'video', url: '/api/jobs/x/assets/vid/file', duration: 30, width: 1920, height: 1080 },
      { id: 'logo', name: 'logo', type: 'image', url: '/api/jobs/x/assets/logo/file.png' },
      { id: 'bgm', name: 'bgm.mp3', type: 'audio', url: '/api/jobs/x/assets/bgm/file', duration: 60 },
    ],
  }
}

const venvPython = path.resolve(
  __dirname,
  '../../../../backend/.venv',
  process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python',
)
// CI's frontend job has no backend venv; any Python 3 can parse the XML.
const python = existsSync(venvPython) ? venvPython : process.platform === 'win32' ? 'python' : 'python3'

/** Parse with Python and return (tag, attributes) for every clip under the gap. */
function parsedClips(xml: string): { tag: string; attrs: Record<string, string> }[] {
  const script =
    'import sys, json, xml.etree.ElementTree as E\n' +
    'root = E.fromstring(sys.stdin.buffer.read())\n' +
    'gap = root.find("./library/event/project/sequence/spine/gap")\n' +
    'print(json.dumps([{"tag": c.tag, "attrs": c.attrib} for c in gap]))'
  const result = spawnSync(python, ['-c', script], {
    input: Buffer.from(xml, 'utf-8'),
    encoding: 'utf8',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
  })
  expect(result.status, result.stderr).toBe(0)
  return JSON.parse(result.stdout)
}

describe('buildFcpXml', () => {
  it('writes well-formed FCPXML with every clip at its frame-exact time and lane', () => {
    const { xml, clips, warnings } = buildFcpXml(project())
    expect(clips).toBe(4)
    expect(xml).toContain('<fcpxml version="1.10">')
    expect(xml).toContain('<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s" width="1920" height="1080"/>')
    expect(xml).toContain('<project name="인터뷰 &amp; 편집">')
    expect(xml).toContain('<sequence format="r1" duration="300/30s"')

    const items = parsedClips(xml)
    expect(items.map((item) => [item.tag, item.attrs.lane, item.attrs.offset, item.attrs.start, item.attrs.duration])).toEqual([
      ['asset-clip', '1', '0s', '0s', '120/30s'],
      ['asset-clip', '1', '120/30s', '180/30s', '60/30s'],
      ['video', '2', '30/30s', '0s', '60/30s'],
      ['asset-clip', '-1', '0s', '0s', '180/30s'],
    ])
    // One <asset> per file, referenced by every clip that uses it.
    expect(xml.match(/<asset id=/g)).toHaveLength(3)
    expect(items[0].attrs.ref).toBe(items[1].attrs.ref)
    expect(xml).toContain('src="file:///interview.mp4"')
    expect(xml).toContain('src="file:///logo.png"')
    expect(warnings.join(' ')).toContain('SRT')
    expect(warnings.join(' ')).toContain('속도')
  })

  it('marks video+audio, still image and sound-only assets correctly', () => {
    const { xml } = buildFcpXml(project())
    expect(xml).toMatch(/name="interview" start="0s" duration="900\/30s" hasVideo="1" hasAudio="1" format="r1"/)
    expect(xml).toMatch(/name="logo" start="0s" duration="0s" hasVideo="1" hasAudio="0" format="r1"/)
    expect(xml).toMatch(/name="bgm" start="0s" duration="1800\/30s" hasVideo="0" hasAudio="1" audioSources/)
  })
})
