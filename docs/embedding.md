# 宿主嵌入小亿数据

本工具仅使用固定合成数据演示资料整理、证据核验与版本发布。宿主可以选择页面、病例和字段；桥不接受真实资料、任意病例对象、模型执行指令或任意脚本。示例入口是 `/host.html`，工具独立入口是 `/index.html`。

## 最小接入

将 `dist` 部署到静态站点，并将发行产物里的 `host-bridge.js` 交给宿主。该文件是无框架依赖的原生 JavaScript ES module。

```html
<iframe id="data-demo" title="小亿数据演示工作台"
  src="https://demo.example/index.html?embed=1&hostOrigin=https%3A%2F%2Fhost.example"
  style="width:100%;height:800px;border:0"></iframe>
<script type="module">
import { createDataDemoHost } from './host-bridge.js';
const iframe = document.querySelector('#data-demo');
const bridge = createDataDemoHost({
  iframe,
  frameOrigin: 'https://demo.example',
  sessionId: 'project-demo-001',
  initialPage: 'overview',
  theme: 'light',
  onEvent(event) {
    if (event.type === 'ready') console.log('工具已就绪');
    if (event.type === 'export') console.log('演示导出', event.payload);
    if (event.type === 'closeRequested') {
      bridge.dispose();
      iframe.remove();
    }
  },
});
// 等待 ready 事件后：
// bridge.navigate('review', { fieldId: 'R-01', patientId: 'P-DEMO-001' });
// bridge.setTheme('dark');
// bridge.reset();
// bridge.switchSession('project-demo-002');
// 离开宿主页面 / 移除 iframe 前调用 bridge.dispose()。
</script>
```

`navigate`、`setTheme`、`reset` 返回是否已发送；未就绪时返回 `false`，调用方可提示稍后重试。`getState()` 返回 `ready/sessionId/instanceId`。`switchSession` 切换明确的会话 ID，不把旧会话的进度复制过去；允许 A → B → A 切回并恢复 A 的演示进度。每条宿主指令（包括 init）都包含该实例严格递增的 seq，延迟或重放的旧 init 不能切回状态。sessionId 是演示状态的命名空间，不是认证凭据。

## 来源、部署和主题

- URL 必须包含 `embed=1` 和精确 `hostOrigin`，例如 `https://host.example`；不能包含路径、尾斜杠、凭据或 `*`。必须在 iframe 中运行嵌入模式。
- 缺省只允许工具自身 origin，`/host.html` 为同源示例。跨域宿主必须在**构建时**配置 `VITE_DEMO_ALLOWED_ORIGINS=https://host.example,https://second.example`，重新构建后部署。不要把密钥放到任何 `VITE_*` 变量中。
- 白名单只能批准允许接入的父窗口；静态部署仍应设置合适的 `Content-Security-Policy: frame-ancestors ...` 响应头。静态页面不能自行替服务器设置这个响应头。
- 此演示支持 `light/dark` 两种主题，使用自身样式令牌。首版不接受任意 CSS 或完整宿主调色板；它不冒充画板的 `yzlingxi.theme/v1` 协议。
- 页面适应 iframe 宽度，高度由宿主容器决定，建议桌面高度至少 720 px；窄容器保留工具内部滚动，不向宿主页面注入全局样式。

## 消息契约

所有消息包含 `protocol: 'xiaoyi.data-demo/v1'` 和 `version: 1`。消息仅在 `event.source === window.parent` / `iframe.contentWindow` 且 `event.origin` 精确匹配后处理。桥验证完整消息结构，拒绝未知字段、未知枚举、无效会话和旧实例。

1. 每次创建工具 bridge 都生成新的 `instanceId`，先发送不含 sessionId 的 `bootstrap`。
2. 宿主 helper 发送随机 `discover.challenge`；工具 bootstrap 回显 challenge。helper 只使用当前 challenge 的响应初始化，避免重载时排队的旧 bootstrap 抢占绑定。
3. 宿主发送 `init`，包含本次 instanceId、合法 sessionId 和可选 `initialPage/theme`。工具不会在明确 init 前恢复嵌入会话，也不会发送业务事件。
4. 工具接受 init 后发送 `ready`。同一会话的新 seq 重复 init 只补发 ready，不重置进度；相同或更小的 seq 被丢弃。不同 sessionId 的新 seq init 触发应用切换（允许切回已用会话），旧会话后续指令及延迟 init 被拒绝。
5. 后续双向消息都携带 sessionId 和 instanceId；所有宿主会话指令还必须携带从 1 起严格递增的安全整数 seq。iframe 重开必须重新握手，旧实例消息无法初始化新实例。

| 方向 | type | payload |
| --- | --- | --- |
| 宿主 → 工具 | `init` | `{initialPage?, theme?}`，sessionId/instanceId 位于消息外层 |
| 宿主 → 工具 | `navigate` | `{page, fieldId?, patientId?}` |
| 宿主 → 工具 | `theme` | `{theme: 'light' \| 'dark'}` |
| 宿主 → 工具 | `reset` | `{}` |
| 工具 → 宿主 | `ready` | 能力列表及 `demoOnly: true` |
| 工具 → 宿主 | `stateChanged` | 应用提供的演示状态摘要 |
| 工具 → 宿主 | `export` | 应用提供的演示导出结果 |
| 工具 → 宿主 | `closeRequested` | 工具申请关闭；宿主负责销毁与清理 |
| 工具 → 宿主 | `error` | 错误码和用户可读说明 |

`page` 仅允许 `overview/intake/processing/review/cases/publish/audit`；`fieldId` 仅允许 `R-01` 至 `R-06`；`patientId` 仅允许 `P-DEMO-001` 至 `P-DEMO-004`。sessionId 必须匹配 `[A-Za-z0-9][A-Za-z0-9._:-]{0,95}`。拒绝的报文会静默丢弃，不触发业务回调。

## 生命周期和验收

关闭、卸载时调用 `dispose()`，移除监听器并作废实例。工具自己的处理定时器和存储逻辑由应用层清理；桥不承诺后台继续运行。session 切换后的状态还原与未完成任务中断应由应用层执行，桥只传递经过验证的初始化指令。

`/host.html` 可验证真实 iframe 打开/关闭/重开、明暗主题、七页定位、R-01 字段跳转、切换新会话并返回上一会话、重置以及导出事件显示。在核验完成、冻结快照并审批后，从工具内点击导出，右侧显示消息正文并启用“下载演示 JSON”，下载内容为 export 事件中的 `payload.data`；未收到有效结果时按钮禁用，切换会话或重置会清空下载结果。这个导出验证的是演示交互，不证明真实科研数据处理或临床算法能力。

## v0.2 资料总览与扫描状态

协议继续使用 `xiaoyi.data-demo/v1`，宿主初始化参数不变。`stateChanged.payload.summary.inventory` 新增资料统计对象，含 `total/pending/processing/processed/failed/restricted` 和按 `text/table/wsi/image` 类型拆分的 `byKind`。原有 `summary.pending` 仍表示待核验字段数，两种计数不能混用。

扫描在工具页面内配置并运行，宿主桥不接收任意文件路径或真实患者资料。关闭／重开、切换会话与重置会清理旧计时器；加载会话时中断扫描并暂停自动扫描，由演示者再次启用。浏览器存储记录升级为 schemaVersion 2，沿用原会话存储键并迁移合法旧数据。
