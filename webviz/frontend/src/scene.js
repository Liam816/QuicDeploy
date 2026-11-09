import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import URDFLoader from 'urdf-loader';
import { poseTransform, setNativeJoints, fitPose } from './kinematics.js';

const colors = [0x67dbb7, 0x78afff];
function dispose(group) {
  group.traverse(object => {
    object.geometry?.dispose();
    const materials = object.material ? (Array.isArray(object.material) ? object.material : [object.material]) : [];
    materials.forEach(material => material.dispose());
  });
  group.clear();
}

export class Workspace {
  constructor(host, onRobotStatus) {
    this.host = host;
    this.onRobotStatus = onRobotStatus;
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#141b21');
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.domElement.setAttribute('aria-label', '双臂预测轨迹三维视图，可用鼠标拖动旋转或滚轮缩放');
    host.appendChild(this.renderer.domElement);
    this.camera = new THREE.PerspectiveCamera(43, 1, .01, 100);
    this.camera.up.set(0, 0, 1);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.minDistance = .15;
    this.controls.maxDistance = 12;
    this.home();
    const grid = new THREE.GridHelper(3, 30, 0x42525e, 0x273640);
    grid.rotation.x = Math.PI / 2;
    this.scene.add(grid, new THREE.AxesHelper(.22));
    this.scene.add(new THREE.HemisphereLight(0xeaf4ff, 0x475363, 2.3));
    const light = new THREE.DirectionalLight(0xffffff, 3);
    light.position.set(2, -2, 4);
    this.scene.add(light);
    this.paths = new THREE.Group();
    this.markers = new THREE.Group();
    this.scene.add(this.paths, this.markers);
    this.robots = [];
    this.showRobot = true;
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(host);
    this.renderer.setAnimationLoop(() => {
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    });
    this.renderer.domElement.addEventListener('webglcontextlost', event => {
      event.preventDefault();
      this.onRobotStatus('WebGL 上下文丢失，请刷新页面');
    });
  }

  resize() {
    const { width, height } = this.host.getBoundingClientRect();
    if (width < 1 || height < 1) return;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  home() {
    this.camera.position.set(1.55, -1.8, 1.35);
    this.controls.target.set(.27, 0, .27);
    this.controls.update();
  }

  view(name) {
    if (name === 'home') return this.home();
    const target = this.controls.target;
    this.camera.position.copy(target).add(name === 'top' ? new THREE.Vector3(0, -.001, 2.1) : new THREE.Vector3(1.9, 0, .35));
    this.controls.update();
  }

  async loadRobot(config) {
    this.config = config;
    if (!config.urdf_url) {
      this.onRobotStatus('未提供 URDF · 仅显示末端轨迹');
      return;
    }
    const manager = new THREE.LoadingManager();
    const assetsLoaded = new Promise(resolve => { manager.onLoad = resolve; });
    const meshErrors = [];
    manager.onError = url => { meshErrors.push(url); this.onRobotStatus(`URDF 网格加载失败：${url}`); };
    const loader = new URDFLoader(manager);
    loader.packages = config.packages;
    try {
      const robot = await loader.loadAsync(config.urdf_url);
      await assetsLoaded;
      const meshCount = [];
      robot.traverse(object => { if (object.isMesh) meshCount.push(object); });
      const missing = [...config.joint_names.flat().filter(name => !robot.joints[name]), ...config.end_links.filter(name => !robot.links[name])];
      this.robotDescription = (meshCount.length ? 'URDF 外观 + 骨架' : 'URDF 骨架 · 无外观网格') +
        (meshErrors.length ? ` · ${meshErrors.length} 个网格加载失败` : '') +
        (missing.length ? ` · 映射缺失：${missing.join(', ')}` : ' · 实测灰 / 当前绿 / 上一帧琥珀');
      for (const kind of ['measured', 'previous', 'current']) {
        const model = robot.clone();
        model.traverse(object => {
          if (!object.isMesh) return;
          const originals = Array.isArray(object.material) ? object.material : [object.material];
          const tinted = originals.map(material => {
            const result = material.clone();
            result.color?.set(kind === 'measured' ? 0xa8b7c5 : kind === 'current' ? 0x67dbb7 : 0xdfa66d);
            result.transparent = kind !== 'measured';
            result.opacity = kind === 'measured' ? 1 : .25;
            result.depthWrite = kind === 'measured';
            return result;
          });
          object.material = Array.isArray(object.material) ? tinted : tinted[0];
        });
        const skeleton = new THREE.Group();
        const material = new THREE.MeshStandardMaterial({ color: kind === 'measured' ? 0x93a5b5 : kind === 'current' ? 0x67dbb7 : 0xdfa66d,
          transparent: true, opacity: kind === 'measured' ? .65 : .5, depthWrite: kind === 'measured' });
        const segments = Object.values(model.joints).map(joint => {
          const segment = new THREE.Mesh(new THREE.CylinderGeometry(.011, .011, 1, 8), material);
          const dot = new THREE.Mesh(new THREE.SphereGeometry(.017, 10, 8), material);
          skeleton.add(segment, dot);
          return { joint, segment, dot };
        });
        this.robots.push({ kind, model, skeleton, segments });
        this.scene.add(model, skeleton);
      }
      this.onRobotStatus(this.robotDescription);
      if (this.last) this.update(...this.last);
    } catch (error) {
      this.onRobotStatus(`URDF 加载失败：${error.message}`);
    }
  }

  drawPath(trajectory, side, previous) {
    const points = trajectory.poses.map(row => new THREE.Vector3(...row.slice(side * 10, side * 10 + 3)));
    if (!points.length) return;
    const geometry = new THREE.BufferGeometry().setFromPoints(points);
    const material = previous
      ? new THREE.LineDashedMaterial({ color: colors[side], transparent: true, opacity: .42, dashSize: .018, gapSize: .012 })
      : new THREE.LineBasicMaterial({ color: colors[side] });
    const line = new THREE.Line(geometry, material);
    line.computeLineDistances();
    this.paths.add(line);
    const dots = new THREE.Points(geometry.clone(), new THREE.PointsMaterial({ color: colors[side], size: previous ? .005 : .009, transparent: true, opacity: previous ? .3 : .8 }));
    this.paths.add(dots);
  }

  drawPose(row, side, previous) {
    const { position, quaternion, gripper } = poseTransform(row, side * 10);
    const group = new THREE.Group();
    group.position.copy(position);
    group.quaternion.copy(quaternion);
    const material = new THREE.MeshBasicMaterial({ color: colors[side], transparent: true, opacity: previous ? .35 : 1 });
    const ball = new THREE.Mesh(new THREE.SphereGeometry(previous ? .009 : .013, 12, 10), material);
    group.add(ball, new THREE.AxesHelper(previous ? .045 : .075));
    for (const sign of [-1, 1]) {
      const finger = new THREE.Mesh(new THREE.BoxGeometry(.008, .008, .035), material);
      finger.position.set(0, sign * Math.max(.004, gripper / 2), .023);
      group.add(finger);
    }
    this.markers.add(group);
  }

  update(current, previous, robot, options) {
    this.last = [current, previous, robot, options];
    dispose(this.paths);
    dispose(this.markers);
    const key = options.trajectory;
    const rows = current?.[key]?.poses ?? [];
    const index = Math.min(options.index, Math.max(0, rows.length - 1));
    const selected = rows[index];
    let previousRow = null;
    if (previous?.[key]?.poses?.length && selected) {
      const times = previous[key].times;
      const t = current[key].times[index];
      let nearest = times.reduce((best, value, i) => Math.abs(value - t) < Math.abs(times[best] - t) ? i : best, 0);
      // Compare at the same absolute policy time, clamped to previous coverage.
      previousRow = previous[key].poses[nearest];
    }
    for (let side = 0; side < 2; side++) {
      if (current?.[key]) this.drawPath(current[key], side, false);
      if (previous?.[key] && options.previous) this.drawPath(previous[key], side, true);
      if (selected) this.drawPose(selected, side, false);
      if (previousRow && options.previous) this.drawPose(previousRow, side, true);
    }
    const messages = [];
    for (const item of this.robots) {
      const { kind, model, skeleton, segments } = item;
      const row = kind === 'current' ? selected : kind === 'previous' ? previousRow : null;
      const seed = kind === 'measured' ? robot?.joints : kind === 'current' ? current?.joint_state : previous?.joint_state;
      let visible = this.showRobot && (kind === 'measured' || (row && (kind !== 'previous' || options.previous)));
      if (visible && !setNativeJoints(model, seed, this.config.joint_names)) visible = false;
      if (visible && row) {
        const fit = fitPose(model, row, this.config.joint_names, this.config.end_links);
        if (kind === 'current') {
          messages.push(...fit.map((result, side) => result.distance === undefined ? result.reason :
            `${side ? 'R' : 'L'} ${Math.round(result.distance * 1000)} mm / ${(result.angle * 180 / Math.PI).toFixed(1)}°${result.valid ? '' : ' 未收敛'}`));
        }
      }
      model.visible = !!visible;
      skeleton.visible = !!visible;
      if (!visible) continue;
      model.updateMatrixWorld(true);
      segments.forEach(({ joint, segment, dot }) => {
        const end = joint.getWorldPosition(new THREE.Vector3());
        const start = joint.parent.getWorldPosition(new THREE.Vector3());
        const direction = end.clone().sub(start);
        segment.visible = direction.length() > .0001;
        segment.position.copy(start).add(end).multiplyScalar(.5);
        segment.scale.y = direction.length();
        segment.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
        dot.position.copy(end);
      });
    }
    if (this.robots.length) this.onRobotStatus(`${this.robotDescription}${messages.length ? ' · 预览 IK：' + messages.join(' · ') : ''}`);
  }

  setRobotVisible(value) {
    this.showRobot = value;
    if (this.last) this.update(...this.last);
  }
}
