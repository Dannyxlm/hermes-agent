import assert from 'node:assert/strict'
import fs from 'node:fs/promises'
import path from 'node:path'
import { test } from 'node:test'
import { fileURLToPath } from 'node:url'
import vm from 'node:vm'

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

async function loadDesktop(name) {
  const contributions = []
  const disposers = []
  const opened = []
  const styles = []
  const profiles = [{ name: 'default', is_default: true }, { name: 'synthetic', is_default: false }]
  const sdk = {
    host: {
      state: { activeSessionId: { get: () => null }, profile: { get: () => 'default' } },
      profiles: { list: async () => ({ profiles }) },
      profileRoutes: async () => [{ profile: 'synthetic', primary: true, connectionId: 'local' }],
      newChat: route => opened.push(route),
      composer: { getDraft: async () => '' },
      notify: () => {}
    },
    COMPOSER_AREAS: { leading: 'composer.leading' },
    PALETTE_AREA: 'palette',
    TRANSCRIPT_DIRECTIVE_AREA: 'transcript.directive',
    profileColor: () => 'synthetic-color',
    usePluginI18n: () => key => key,
    useQuery: () => ({}),
    useValue: atom => atom.get(),
    queryClient: { invalidateQueries: () => {} }
  }
  for (const name of ['DropdownMenu', 'DropdownMenuContent', 'DropdownMenuItem', 'DropdownMenuSeparator', 'DropdownMenuTrigger', 'Tip', 'Button']) {
    sdk[name] = () => null
  }
  const react = { createElement: (type, props) => ({ type, props }) }
  const context = vm.createContext({
    console,
    document: {
      createElement: () => ({ setAttribute() {}, remove() { styles.pop() } }),
      head: { appendChild: style => styles.push(style) }
    }
  })
  const modules = new Map()
  for (const [specifier, values] of Object.entries({
    '@hermes/plugin-sdk': sdk,
    react,
    'react/jsx-runtime': { jsx: react.createElement, jsxs: react.createElement }
  })) {
    modules.set(specifier, new vm.SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value)
    }, { context }))
  }
  const source = await fs.readFile(path.join(repo, 'plugins', name, 'desktop/plugin.js'), 'utf8')
  const module = new vm.SourceTextModule(source, { context })
  await module.link(specifier => {
    assert.ok(modules.has(specifier), `Unreviewed runtime import ${specifier}`)
    return modules.get(specifier)
  })
  await module.evaluate()
  const plugin = module.namespace.default
  assert.equal(plugin.defaultEnabled, false)
  plugin.register({
    register: value => contributions.push(value),
    registerMany: values => contributions.push(...values),
    onDispose: value => disposers.push(value),
    onEvent() {},
    i18n: { register() {}, t: key => key }
  })
  await new Promise(resolve => setImmediate(resolve))
  return { contributions, disposers, opened, styles }
}

test('Draft Cards registers its directive and cleans up its only DOM write', async () => {
  const loaded = await loadDesktop('draft-cards')
  assert.equal(loaded.contributions.length, 1)
  assert.equal(loaded.contributions[0].area, 'transcript.directive')
  assert.equal(loaded.contributions[0].data.name, 'draft-card')
  assert.equal(loaded.styles.length, 1)
  for (const dispose of loaded.disposers) dispose()
  assert.equal(loaded.styles.length, 0)
})

test('Profile Picker registers composer and profile-routed new-chat actions', async () => {
  const loaded = await loadDesktop('profile-picker')
  assert.ok(loaded.contributions.some(item => item.area === 'composer.leading'))
  const command = loaded.contributions.find(item => item.id === 'new-in-synthetic')
  assert.ok(command)
  command.data.run()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(loaded.opened.length, 1)
  assert.equal(loaded.opened[0].profile, 'synthetic')
  assert.equal(loaded.opened[0].connectionId, 'local')
})

test('Duty Board uses the web-dashboard registration contract', async () => {
  const registrations = []
  const context = vm.createContext({
    window: {
      __HERMES_PLUGIN_SDK__: { React: { createElement() {} }, hooks: {} },
      __HERMES_PLUGINS__: { register: (name, component) => registrations.push({ name, component }) }
    },
    Intl,
    Map,
    Set
  })
  const source = await fs.readFile(path.join(repo, 'plugins/duty-board/dashboard/dist/index.js'), 'utf8')
  vm.runInContext(source, context)
  assert.equal(registrations.length, 1)
  assert.equal(registrations[0].name, 'duty-board')
  assert.equal(typeof registrations[0].component, 'function')
})
