import { app } from "/scripts/app.js";
import { createSetting } from "./config_service.js";
import { ComfyWidgets } from "../../../scripts/widgets.js";

const editorPathPrefixId = "easy_nodes.EditorPathPrefix";

function resizeShowValueWidgets(node, numValues, app) {
  const numShowValueWidgets = (node.showValueWidgets?.length ?? 0);
  numValues = Math.max(numValues, 0);

  if (numValues > numShowValueWidgets) {
    for (let i = numShowValueWidgets; i < numValues; i++) {
      const showValueWidget = ComfyWidgets["STRING"](node, `output${i}`, ["STRING", { multiline: true }], app).widget;
      showValueWidget.inputEl.readOnly = true;
      if (!node.showValueWidgets) {
        node.showValueWidgets = [];
      }
      node.showValueWidgets.push(showValueWidget);
    }
  } else if (numValues < numShowValueWidgets) {
    const removedWidgets = node.showValueWidgets.splice(numValues);
    node.widgets.splice(node.origWidgetCount + numValues);

    // Remove the detached widgets from the DOM
    removedWidgets.forEach(widget => {
      widget.inputEl.parentNode.removeChild(widget.inputEl);
    });
  }
}

app.registerExtension({
  name: "EasyNodes",
  async setup() {
    createSetting(
      "easy_nodes.SourcePathPrefix",
      "🪄 Stack trace remove prefix (common prefix to remove, e.g '/home/user/project/')",
      "text",
      ""
    );
    createSetting(
      editorPathPrefixId,
      "🪄 Stack trace link prefix (insert this in stack traces to make them clickable, e.g. 'vscode://vscode-remote/wsl+Ubuntu')",
      "text",
      ""
    );
    createSetting(
      "easy_nodes.ReloadOnEdit",
      "🪄 Auto-reload EasyNodes source files on edits.",
      "boolean",
      false,
    );
  },
  async beforeRegisterNodeDef(nodeType, nodeData, app) {
    if (nodeData?.description.startsWith("easy_nodes")) {
      const lines = nodeData.description.split('\n').slice(1);
      var fgColor = null;
      var bgColor = null;
      var nodeSource = null;
      var outputLines = [];
      for (const line of lines) {
        if (line.startsWith('ComfyUINodeColor=')) {
          const color = line.split('=')[1];
          fgColor = color;
        } else if (line.startsWith('ComfyUINodeBgColor=')) {
          const color = line.split('=')[1];
          bgColor = color;
        } else if (line.startsWith('NodeSource=')) {
          const source = line.split('=')[1];
          nodeSource = source;
        } else {
          outputLines.push(line);
        }
      }
      nodeData.description = outputLines.join('\n');

      const editorPathPrefix = app.ui.settings.getSettingValue(editorPathPrefixId);

      function applyColorsAndSource() {
        if (fgColor) {
          this.color = fgColor;
        }
        if (bgColor) {
          this.bgcolor = bgColor;
        }
        if (nodeSource && editorPathPrefix) {
          this.sourceLoc = editorPathPrefix + nodeSource;
        }
        this.description = nodeData.description;
      }

      // Apply colors and source location when the node is created
      const onNodeCreated = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        onNodeCreated?.apply(this, arguments);
        applyColorsAndSource.call(this);
        this.origWidgetCount = this.widgets?.length ?? 0;
      };

      // Apply colors and source location when configuring the node
      const onConfigure = nodeType.prototype.onConfigure;
      nodeType.prototype.onConfigure = function () {
        onConfigure?.apply(this, arguments);
        applyColorsAndSource.call(this);

        this.origWidgetCount = this.widgets?.length ?? 0;
        const widgetValsLength = this.widgets_values?.length ?? 0;
        
        const numShowVals = widgetValsLength - this.origWidgetCount;
        console.log("numShowVals", numShowVals, widgetValsLength, this.origWidgetCount);
        resizeShowValueWidgets(this, numShowVals, app);

        for (let i = 0; i < numShowVals; i++) {
          this.showValueWidgets[i].value = this.widgets_values[this.origWidgetCount + i];
        }
      };

      const onExecuted = nodeType.prototype.onExecuted;
      nodeType.prototype.onExecuted = function (message) {
        onExecuted?.apply(this, [message]);

        const numShowVals = message.text.length;

        console.log(this.id, "onExecuted", numShowVals, message.text.length, this.origWidgetCount);

        resizeShowValueWidgets(this, numShowVals, app);

        for (let i = 0; i < numShowVals; i++) {
          this.showValueWidgets[i].value = message.text[i];
        }

        this.setSize( this.computeSize() );
        this.setDirtyCanvas(true, true);
        app.graph.setDirtyCanvas(true, true);
      }
    }
  },
});
