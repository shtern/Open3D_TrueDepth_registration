import open3d as o3d
import numpy as np
import cv2 as cv
import json
import struct
import base64

class ImageDepth:
    def __init__(self,
        calibration_file,
        image_file,
        depth_file,
        width=640,
        height=480,
        max_distance=0.5,
        distance_threshold=0.1,
        normal_radius=0.1):

        self.image_file = image_file
        self.calibration_file = calibration_file
        self.depth_file = depth_file
        self.width = width
        self.height = height
        self.max_distance = max_distance
        self.distance_threshold = distance_threshold
        self.normal_radius = normal_radius

        self.load_calibration(calibration_file)
        self.create_undistortion_lookup()

        self.load_image(image_file)
        self.load_depth(depth_file, max_distance)

    def load_calibration(self, file):
        with open(file) as f:
            data = json.load(f)

            lensDistortionLookupBase64 = data["lensDistortionLookup"]
            inverseLensDistortionLookupBase64 = data["inverseLensDistortionLookup"]
            lensDistortionLookupByte = base64.decodebytes(lensDistortionLookupBase64.encode("ascii"))
            inverseLensDistortionLookupByte = base64.decodebytes(inverseLensDistortionLookupBase64.encode("ascii"))

            lensDistortionLookup = struct.unpack(f"<{len(lensDistortionLookupByte)//4}f",lensDistortionLookupByte)
            inverseLensDistortionLookup = struct.unpack(f"<{len(inverseLensDistortionLookupByte)//4}f",inverseLensDistortionLookupByte)

            self.lensDistortionLookup = lensDistortionLookup
            self.inverseLensDistortionLookup = inverseLensDistortionLookup

            self.intrinsic = np.array(data["intrinsic"]).reshape((3,3))
            self.intrinsic = self.intrinsic.transpose()

            self.scale = float(self.width) / data["intrinsicReferenceDimensionWidth"]
            self.intrinsic[0,0] *= self.scale
            self.intrinsic[1,1] *= self.scale
            self.intrinsic[0,2] *= self.scale
            self.intrinsic[1,2] *= self.scale

    def create_undistortion_lookup(self):
        xy_pos = [(x,y) for y in range(0, self.height) for x in range(0, self.width)]
        xy = np.array(xy_pos, dtype=np.float32).reshape(-1,2)

        # subtract center
        center = self.intrinsic[0:1, 2]
        xy -= center

        # calc radius from center
        r = np.sqrt(xy[:,0]**2 + xy[:,1]**2)

        # normalize radius
        max_r = np.max(r)
        norm_r = r / max_r

        # interpolate the magnification
        table = self.inverseLensDistortionLookup
        num = len(table)
        magnification = 1.0 + np.interp(norm_r*num, np.arange(0, num), table)

        # unit vector
        magnititude = np.expand_dims(np.sqrt(xy[:,0]**2 + xy[:,1]**2), 1)
        unit_xy = xy / magnititude

        new_xy = unit_xy * magnititude * np.expand_dims(magnification, 1)
        new_xy += center

        self.map_x = new_xy[:,0].reshape((self.height, self.width)).astype(np.float32)
        self.map_y = new_xy[:,1].reshape((self.height, self.width)).astype(np.float32)

    def load_depth(self, file, max_distance):
        depth = np.fromfile(file, dtype='float16').astype(np.float32)

        # vectorize version, faster
        # all possible (x,y) position
        xy = [(x,y) for y in range(0, self.height) for x in range(0, self.width)]
        xy = np.array(xy).astype(np.float32)

        # remove bad values
        no_nan = np.invert(np.isnan(depth))
        good_range = depth < max_distance
        idx = np.logical_and(no_nan, good_range)
        xy = xy[np.where(idx)]

        # mask out depth buffer
        self.depth_map = depth
        self.depth_map[np.where(idx == False)] = -1000
        self.depth_map = self.depth_map.reshape((self.height, self.width, 1))
        self.depth_map_undistort = cv.remap(self.depth_map, self.map_x, self.map_y, cv.INTER_LINEAR)

        per = float(np.sum(idx==True))/len(depth)
        print(f"Processing {file}, keeping={np.sum(idx==True)}/{len(depth)} ({per:.3f}) points")

        depth = np.expand_dims(self.depth_map_undistort.flatten()[np.where(idx)],1)

        # project to 3D
        xyz, _, good_idx = self.project3d(xy)
        xyz = xyz[good_idx]

        self.pcd = o3d.geometry.PointCloud()
        self.pcd.points = o3d.utility.Vector3dVector(xyz)

        # calc normal, required for ICP point-to-plane
        self.pcd.estimate_normals(search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=self.normal_radius, max_nn=10))

    def load_image(self, file):
        self.img = np.fromfile(file, dtype='uint8')
        self.img = self.img.reshape((self.height, self.width, 4))
        self.img = self.img[:,:,0:3]

        # swap RB
        #self.img = img[:,:,[2,1,0]]
        self.gray = cv.cvtColor(self.img, cv.COLOR_RGB2GRAY)

        self.img_undistort = cv.remap(self.img, self.map_x, self.map_y, cv.INTER_LINEAR)
        self.gray_undistort = cv.remap(self.gray, self.map_x, self.map_y, cv.INTER_LINEAR)

    def project3d(self, pts):
        # epxect pts to be Nx2

        xy = np.round(pts).astype(np.int)

        fx = self.intrinsic[0,0]
        fy = self.intrinsic[1,1]
        cx = self.intrinsic[0,2]
        cy = self.intrinsic[1,2]

        depths = self.depth_map_undistort[xy[:,1], xy[:,0]]
        depths = np.expand_dims(depths, 1)
        good_idx = np.where(depths > 0)[0]

        pts -= np.array([cx, cy])
        pts /= np.array([fx, fy])
        pts *= depths
        pts = np.hstack((pts, depths))

        return pts, xy, good_idx