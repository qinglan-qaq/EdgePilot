import { describe, expect, it } from 'vitest'

import type { WorkflowDsl } from '../src/api/workflows'
import {
  connectFlowNodes,
  createFlowNode,
  dslToFlow,
  flowToDsl,
  stableStringify,
  type FlowNode,
} from '../src/components/workflow/editor-model'

const origin = { x: 10, y: 20 }

describe('工作流编辑器模型', () => {
  it('按类型生成唯一节点 id，并拒绝第二个 start', () => {
    const nodes: FlowNode[] = []
    const types = ['start', 'agent', 'condition', 'approval', 'agent', 'end', 'end'] as const

    for (const type of types) {
      const node = createFlowNode(type, nodes, origin)
      expect(node).not.toBeNull()
      nodes.push(node as FlowNode)
    }

    expect(nodes.map((node) => node.id)).toEqual([
      'start',
      'agent_1',
      'condition_1',
      'approval_1',
      'agent_2',
      'end',
      'end_2',
    ])
    expect(new Set(nodes.map((node) => node.id)).size).toBe(nodes.length)

    const secondStart = createFlowNode('start', nodes, origin)
    expect(secondStart).toBeNull()
  })

  it('六种节点生成正确的默认字段', () => {
    const start = createFlowNode('start', [], origin) as FlowNode
    expect(start.data.fields).toEqual({ timeout_ms: 30_000 })
    expect(start.data.extra).toEqual({})

    const agent = createFlowNode('agent', [start], origin) as FlowNode
    expect(agent.data.fields).toEqual({ timeout_ms: 30_000, agent: '', max_retries: 2 })

    const tool = createFlowNode('tool', [start, agent], origin) as FlowNode
    expect(tool.data.fields).toEqual({ timeout_ms: 30_000, tool: '', max_retries: 2 })

    const condition = createFlowNode('condition', [start, agent, tool], origin) as FlowNode
    expect(condition.data.fields).toEqual({ timeout_ms: 30_000, expression: '' })

    const approval = createFlowNode(
      'approval',
      [start, agent, tool, condition],
      origin,
    ) as FlowNode
    expect(approval.data.fields).toEqual({
      timeout_ms: 86_400_000,
      title: '',
      approvers: [],
    })

    const end = createFlowNode('end', [start, agent, tool, condition, approval], origin) as FlowNode
    expect(end.data.fields).toEqual({ timeout_ms: 30_000 })
  })

  it('DSL 往返完整保留 position 与未知字段', () => {
    const dsl: WorkflowDsl = {
      nodes: [
        { id: 'start', type: 'start', timeout_ms: 30_000, position: { x: 10, y: 20 } },
        {
          id: 'agent_1',
          type: 'agent',
          timeout_ms: 45_000,
          agent: 'planner',
          max_retries: 3,
          input_map: { task: '$input.task', count: 2, flag: true },
          future_flag: 'keep-me',
          position: { x: 320, y: 80 },
        },
        { id: 'end', type: 'end', timeout_ms: 30_000, position: { x: 640, y: 80 } },
      ],
      edges: [
        { id: 'e1', from: 'start', to: 'agent_1', future_edge: 'keep-me' },
        { id: 'e2', from: 'agent_1', to: 'end' },
      ],
    }

    const flow = dslToFlow(dsl)
    expect(flow.nodes).toHaveLength(3)
    expect(flow.edges).toHaveLength(2)

    const roundTripped = flowToDsl(flow.nodes, flow.edges)
    expect(roundTripped.nodes).toEqual(dsl.nodes)
    expect(roundTripped.edges).toEqual(dsl.edges)
  })

  it('condition 边依据 sourceHandle 生成 when，并替换同一分支旧边', () => {
    const start = createFlowNode('start', [], origin) as FlowNode
    const condition = createFlowNode('condition', [start], origin) as FlowNode
    const approval = createFlowNode('approval', [start, condition], origin) as FlowNode
    const end = createFlowNode('end', [start, condition, approval], origin) as FlowNode
    const end2 = createFlowNode('end', [start, condition, approval, end], origin) as FlowNode
    const nodes = [start, condition, approval, end, end2]

    const trueBranch = connectFlowNodes(nodes, [], {
      source: 'condition_1',
      target: 'approval_1',
      sourceHandle: 'true',
    })
    expect(trueBranch.error).toBeNull()
    expect(trueBranch.edges).toHaveLength(1)
    expect(trueBranch.edges[0]?.data.when).toBe('true')

    const falseBranch = connectFlowNodes(nodes, trueBranch.edges, {
      source: 'condition_1',
      target: 'end',
      sourceHandle: 'false',
    })
    expect(falseBranch.error).toBeNull()
    expect(falseBranch.edges).toHaveLength(2)

    const replaced = connectFlowNodes(nodes, falseBranch.edges, {
      source: 'condition_1',
      target: 'end_2',
      sourceHandle: 'true',
    })
    expect(replaced.error).toBeNull()
    expect(replaced.edges).toHaveLength(2)
    expect(
      replaced.edges.find((edge) => edge.data.when === 'true')?.target,
    ).toBe('end_2')
    expect(
      replaced.edges.find((edge) => edge.data.when === 'false')?.target,
    ).toBe('end')

    const dsl = flowToDsl(nodes, replaced.edges)
    expect(dsl.edges.find((edge) => edge.to === 'end_2')?.when).toBe('true')
    expect(dsl.edges.find((edge) => edge.from === 'condition_1' && edge.to === 'end')?.when).toBe(
      'false',
    )
  })

  it('非 condition 节点拒绝重复边与非法端点', () => {
    const start = createFlowNode('start', [], origin) as FlowNode
    const agent = createFlowNode('agent', [start], origin) as FlowNode
    const end = createFlowNode('end', [start, agent], origin) as FlowNode
    const nodes = [start, agent, end]

    const first = connectFlowNodes(nodes, [], {
      source: 'start',
      target: 'agent_1',
    })
    expect(first.error).toBeNull()

    const duplicate = connectFlowNodes(nodes, first.edges, {
      source: 'start',
      target: 'agent_1',
    })
    expect(duplicate.error).toContain('已存在')
    expect(duplicate.edges).toHaveLength(1)

    const selfLoop = connectFlowNodes(nodes, first.edges, {
      source: 'agent_1',
      target: 'agent_1',
    })
    expect(selfLoop.error).toContain('自身')

    const endOutgoing = connectFlowNodes(nodes, first.edges, {
      source: 'end',
      target: 'agent_1',
    })
    expect(endOutgoing.error).toContain('end 节点')

    const startIncoming = connectFlowNodes(nodes, first.edges, {
      source: 'agent_1',
      target: 'start',
    })
    expect(startIncoming.error).toContain('start 节点')
  })

  it('缺失 position 时按 start 层级自动布局且结果确定', () => {
    const dsl: WorkflowDsl = {
      nodes: [
        { id: 'start', type: 'start', timeout_ms: 30_000 },
        { id: 'agent_1', type: 'agent', timeout_ms: 30_000, agent: 'planner' },
        { id: 'end', type: 'end', timeout_ms: 30_000 },
      ],
      edges: [
        { from: 'start', to: 'agent_1' },
        { from: 'agent_1', to: 'end' },
      ],
    }

    const first = dslToFlow(dsl)
    const second = dslToFlow(dsl)
    const positionOf = (nodes: FlowNode[], id: string) =>
      nodes.find((node) => node.id === id)?.position

    const startPosition = positionOf(first.nodes, 'start')
    const agentPosition = positionOf(first.nodes, 'agent_1')
    const endPosition = positionOf(first.nodes, 'end')

    expect(startPosition).toEqual({ x: 80, y: 80 })
    expect(agentPosition?.x).toBeGreaterThan(startPosition?.x ?? 0)
    expect(endPosition?.x).toBeGreaterThan(agentPosition?.x ?? 0)
    expect(first.nodes.map((node) => node.position)).toEqual(
      second.nodes.map((node) => node.position),
    )
    for (const node of first.nodes) {
      expect(Number.isFinite(node.position.x)).toBe(true)
      expect(Number.isFinite(node.position.y)).toBe(true)
    }
  })

  it('stableStringify 忽略对象键顺序', () => {
    expect(stableStringify({ b: 2, a: 1 })).toBe(stableStringify({ a: 1, b: 2 }))
    expect(stableStringify({ a: 1, b: undefined })).toBe(stableStringify({ a: 1 }))
  })
})
