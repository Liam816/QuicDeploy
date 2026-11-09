import * as THREE from 'three';

export function poseTransform(row, base) {
  const x = new THREE.Vector3(...row.slice(base + 3, base + 6)).normalize();
  const y = new THREE.Vector3(...row.slice(base + 6, base + 9));
  y.addScaledVector(x, -x.dot(y)).normalize();
  const z = new THREE.Vector3().crossVectors(x, y).normalize();
  return {
    position: new THREE.Vector3(...row.slice(base, base + 3)),
    quaternion: new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(x, y, z)),
    gripper: row[base + 9] / 1000,
  };
}

export function setNativeJoints(robot, joints, names) {
  if (!Array.isArray(joints) || joints.length !== 14 || names.flat().some(name => !robot.joints[name])) return false;
  names.forEach((arm, side) => arm.forEach((name, i) => robot.setJointValue(name, joints[side * 7 + i] * Math.PI / 180)));
  robot.updateMatrixWorld(true);
  return true;
}

function solve(matrix, vector) {
  const a = matrix.map((row, i) => [...row, vector[i]]);
  for (let k = 0; k < vector.length; k++) {
    let pivot = k;
    for (let i = k + 1; i < a.length; i++) if (Math.abs(a[i][k]) > Math.abs(a[pivot][k])) pivot = i;
    [a[k], a[pivot]] = [a[pivot], a[k]];
    if (Math.abs(a[k][k]) < 1e-12) return vector.map(() => 0);
    const scale = a[k][k];
    for (let j = k; j <= vector.length; j++) a[k][j] /= scale;
    for (let i = 0; i < a.length; i++) {
      if (i === k) continue;
      const factor = a[i][k];
      for (let j = k; j <= vector.length; j++) a[i][j] -= factor * a[k][j];
    }
  }
  return a.map(row => row[vector.length]);
}

// Browser-only damped least-squares IK. Never sent back to the robot.
export function fitPose(robot, row, names, endLinks) {
  const results = [];
  names.forEach((jointNames, side) => {
    const joints = jointNames.map(name => robot.joints[name]);
    const end = robot.links[endLinks[side]];
    if (!end || joints.some(joint => !joint || !['revolute', 'continuous', 'prismatic'].includes(joint.jointType))) {
      results.push({ valid: false, reason: 'URDF 关节/末端映射不匹配' });
      return;
    }
    const target = poseTransform(row, side * 10);
    let distance = Infinity, angle = Infinity;
    for (let iteration = 0; iteration < 55; iteration++) {
      robot.updateMatrixWorld(true);
      const position = end.getWorldPosition(new THREE.Vector3());
      const orientation = end.getWorldQuaternion(new THREE.Quaternion());
      const delta = target.quaternion.clone().multiply(orientation.invert()).normalize();
      if (delta.w < 0) delta.set(-delta.x, -delta.y, -delta.z, -delta.w);
      angle = 2 * Math.acos(THREE.MathUtils.clamp(delta.w, -1, 1));
      const sine = Math.sqrt(Math.max(0, 1 - delta.w * delta.w));
      const rotation = sine < 1e-8 ? new THREE.Vector3() : new THREE.Vector3(delta.x, delta.y, delta.z).multiplyScalar(angle / sine);
      const translation = target.position.clone().sub(position);
      distance = translation.length();
      if (distance < .002 && angle < .035) break;
      const weight = .35;
      const error = [...translation.toArray(), ...rotation.multiplyScalar(weight).toArray()];
      const columns = joints.map(joint => {
        const axis = joint.axis.clone().transformDirection(joint.matrixWorld);
        if (joint.jointType === 'prismatic') return [...axis.toArray(), 0, 0, 0];
        const offset = position.clone().sub(joint.getWorldPosition(new THREE.Vector3()));
        return [...new THREE.Vector3().crossVectors(axis, offset).toArray(), ...axis.multiplyScalar(weight).toArray()];
      });
      const normal = Array.from({ length: 6 }, (_, i) => Array.from({ length: 6 }, (_, j) =>
        columns.reduce((sum, column) => sum + column[i] * column[j], 0) + (i === j ? .0005 : 0)));
      const solution = solve(normal, error);
      joints.forEach((joint, i) => {
        const increment = THREE.MathUtils.clamp(columns[i].reduce((sum, value, j) => sum + value * solution[j], 0), -.18, .18);
        joint.setJointValue(joint.jointValue[0] + increment);
      });
    }
    results.push({ valid: distance < .008 && angle < .1, distance, angle });
  });
  robot.updateMatrixWorld(true);
  return results;
}
