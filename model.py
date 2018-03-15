import numpy as np
import cv2
from scipy.spatial.distance import euclidean
from skimage import filters, exposure, feature, morphology, measure, img_as_float, segmentation
from collections import deque
from pandas import ewma, ewmstd, Series
from itertools import count, islice


class Pupil_Model(object):

    def __init__(self, x, y, rad, st_memory=100, lt_ratio = 10, hl = 50):
        # We initialize with circle params for now because that's all we can trace on the image
        # st_memory = how many sequential frames we keep
        # lt_ratio = how many frames to skip before stashing a long-term estimate
        # (lt params are just as long as st_memory)
        # we want to use the st params to calculate the stdev for filtering points
        # and the lt params for setting constraints.
        # hl = half-life for the exponentially weighted moving average

        self.lt_ratio = lt_ratio
        self.hl = hl

        # we store the initial params to make sanity checks
        x, y, rad = float(x), float(y), float(rad)
        self.initial_params = (x, y, rad, rad, 0.)

        # lists of deque to store the parameter history
        self.st_params = [deque(maxlen=st_memory) for i in range(5)]
        self.lt_params = [deque(maxlen=st_memory) for i in range(5)]

        # store the longest axis - aka the pupil diameter
        self.pupil_diam = []

        # store the number of points & residuals we get from edge detection to estimate frame quality
        self.n_points = deque(maxlen=st_memory)
        self.resids   = deque(maxlen=st_memory)

        # store the standard deviation used to include/exclude points
        # we initialize to 1/8 of the radius of the circle for no particular reason for the first n frames
        self.stdev = 10
        self.stdevs = []
        self.mults = []

        # Make our ellipse model, set, and stash initial parameters
        self.model = measure.EllipseModel()
        self.model.params = (x, y, rad, rad, 0) # x, y, a, b, theta

        # Make a frame-by-frame ellipse model to perform computations to update lt ellipse
        self.st_model = measure.EllipseModel()
        self.st_model.params = (x, y, rad, rad, 0) # x, y, a, b, theta

        # counter to keep track of frames
        self.frame_counter = count()
        self.n_frames = 0

        self.obl = []

        # keep track of the frames where the circle fit failed
        self.failed_frames = []

        self.rmult = []
        self.point_mult = []


    def update(self, points, frame):
        # update the model using detected edges from the the most recent frame
        self.n_frames = self.frame_counter.next()

        ret, f_points = self.contour_fit(frame)

        # select points within (self.stdev) of the model
        f_points = np.concatenate((f_points, self.filter_points(points)))


        # get quality metrics, n points, param changes, oblongity
        self.n_points.append(f_points.shape[0])
        point_mult = np.clip(float(f_points.shape[0])/np.mean(self.n_points), 0, 1)
        self.point_mult.append(point_mult)

        # residuals
        residuals = np.mean(self.model.residuals(f_points)**2)
        self.resids.append(residuals)
        if residuals > 0:
            residual_mult = np.clip(np.mean(self.resids)/residuals, 0,1)
        else:
            residual_mult = 0

        self.rmult.append(residual_mult)

        # oblongity
        ret = self.st_model.estimate(f_points)

        # reform params so maj/minor axis are consistent
        st_params = self.st_model.params

        if ret:
            oblongity = np.min(self.st_model.params[2:4])/np.max(self.st_model.params[2:4])
            oblongity = np.clip(oblongity, 0, 1)
        else:
            oblongity = 0

        self.obl.append(oblongity)

        # combine multipliers
        combo_mult = point_mult * residual_mult * oblongity

        self.mults.append(combo_mult)

        # param changes

        param_diff = [p2-p1 for p1, p2 in zip(self.model.params, self.st_model.params)]
        #diff_mag = [p/(p**2) if p>1 else 1 for p in param_diff]
        param_diff = [p*combo_mult*.8 if abs(p)<10 else 0 for p in param_diff ]

        # theta changes really quickly so we esp damp that one

        params = [p1+p2 for p1, p2 in zip(self.model.params, param_diff)]
        params[4] = self.st_model.params[4]

        # drift back to init if we get lost

        orig_diff = []

        self.model.params = params

        self.pupil_diam.append(np.max(params[2:4]))


        #self.stash_params(ret)

        #self.update_std(f_points)

        #if self.stdev > 20:
        #    self.backtrack_model()




    def filter_points(self, points):
        # find only those points that are within a standard deviation of our model
        # with respect to https://stackoverflow.com/questions/37031356/check-if-points-are-inside-ellipse-faster-than-contains-point-method

        # get model params handy
        x, y, a, b, t = self.model.params

        # make min/max ellipse axes from standard dev and normalize
        # then pick the more restrictive values
        min_r = np.max([(a-self.stdev)/a, (b-self.stdev)/b])
        max_r = np.min([(a+self.stdev)/a, (b+self.stdev)/b])

        # split points into x and y coords
        pts_x, pts_y = points[:,0], points[:,1]

        cos_e = np.cos(t)
        sin_e = np.sin(t)

        # get distance from center of ellipse
        dist_x = pts_x - x
        dist_y = pts_y - y

        # transform so that coordinates aligned w/ major/minor ax
        tf_x = dist_x*cos_e - dist_y*sin_e
        tf_y = dist_x*sin_e + dist_y*cos_e

        # get normalized distance from center
        norm_dist = (tf_x**2/a**2) + (tf_y**2/b**2)

        # get the points in error margin
        good_pts = np.logical_and(norm_dist > min_r, norm_dist < max_r)

        return points[good_pts, :]

    def augment_points(self, filtered_pts):
        # generate points from the current model, use them and new points to fit the model
        # for now let's try to use 125% of the mean points for no particular reason
        # so if we get a particularly bad frame, we use more points from the previous model

        mean_pts = np.mean(self.n_points)

        model_pts = (1.-self.n_points[-1]/mean_pts) * mean_pts

        # if we got a bunch of good points, just use those
        if model_pts <= 0:
            return filtered_pts
        else:
            # generate set of thetas to predict
            thetas = np.linspace(0, np.pi*2, num=int(model_pts), endpoint=False)

            pred_pts = self.model.predict_xy(thetas)

            return np.concatenate([filtered_pts, pred_pts])


    def contour_fit(self, frame):
        # to additionally filter spurious edges that throw the model off,
        # we do another round of fitting with active contours
        thetas = np.linspace(0, np.pi*2, num=100, endpoint=False)
        pred_pts = self.model.predict_xy(thetas)
        cont_pts = segmentation.active_contour(filters.gaussian(frame, 2), pred_pts,
                                             beta=0.5, bc='periodic')

        # filter points too far from the model -- who knows what the snakes caught
        filt_pts = self.filter_points(cont_pts)

        ret = self.st_model.estimate(filt_pts)
        return ret, filt_pts



    def stash_params(self, ret):
        # if the model fails, just dupe the last params
        if ret == False:
            params = [dq[-1] for dq in self.st_params]
            self.failed_frames.append(self.n_frames)
        else:
            params = self.model.params

        # store longest rad as pupil diam
        self.pupil_diam.append(np.max(params[2:4]))

        # if we have hit our lt ratio, update the lt deque
        if self.n_frames % self.lt_ratio == 0 and self.n_frames > 0:
            for param, st_dq, lt_dq in zip(params, self.st_params, self.lt_params):
                st_dq.append(param)
                # append the mean of the last (lt_ratio) param entries
                lt_dq.append(np.mean(list(st_dq)[-self.lt_ratio:]))

        # otherwise we just save this round
        else:
            for param, st_dq in zip(params, self.st_params):
                st_dq.append(param)

    def check_quality(self, filtered_pts):
        # more points, lower residuals are a better estimate
        point_prop = 1./(float(self.n_points[-1])/np.mean(self.n_points))
        self.resids.append(np.mean(self.model.residuals(filtered_pts)**2))
        resid_prop = float(self.resids[-1])/np.mean(self.resids)

        quality_prop = point_prop * resid_prop
        # make this number closer to one to have less influence...
        #quality_prop = ((quality_prop-1.)/2.)+1

        return quality_prop


    def update_std(self, filtered_pts):
        # calc the acceptable error from the point residuals, quantity, and previous param variation

        # for the first (halflife) points, use an arbitrarily set stdev
        if self.n_frames < self.hl:
            param_std = self.initial_params[2]/4.
        else:
            # exponentially weighted stdev of all params except theta
            param_stds = [Series(param).ewm(halflife=10).std().iloc[-1] for param in self.st_params[0:4]]
            param_std = np.mean(param_stds)

        # take ratio of this and last std
        std_prop = param_std/self.stdev

        # adjust the std by the quality of our estimate
        # more points, lower residuals are a better estimate
        point_prop = 1./(float(self.n_points[-1])/np.mean(self.n_points))
        self.resids.append(np.mean(self.model.residuals(filtered_pts)**2))
        resid_prop = float(self.resids[-1])/np.mean(self.resids)

        quality_prop = np.mean([point_prop, resid_prop])
        # make this number closer to one to have less influence...
        quality_prop = ((quality_prop-1.)/2.)+1

        # increment stdev
        self.stdev = self.stdev * std_prop * quality_prop
        self.stdev = np.log(self.stdev)
        self.stdev = np.clip(self.stdev, 0, 20)

        self.stdevs.append(self.stdev)


    def make_points(self, n_pts, splitxy=False, st_mod=False):
        thetas = np.linspace(0, np.pi*2, num=n_pts, endpoint=False)
        if st_mod:
            points = self.st_model.predict_xy(thetas)
        else:
            points = self.model.predict_xy(thetas)
        if not splitxy:
            return points
        else:
            return points[:,0], points[:,1]

class slice_dq(deque):
    # credit to https://stackoverflow.com/a/10003201
    def __getitem__(self, index):
        try:
            return deque.__getitem__(self, index)
        except TypeError:
            return type(self)(islice(self, index.start,
                                     index.stop, index.step))


# c_array = []
# for pt in norm_dist:
#     if min_r < pt < max_r:
#         c_array.append('green')
#     else:
#         c_array.append('black')
#
# ell_patch = patches.Ellipse((x, y), a*2, b*2, angle=np.rad2deg(t), fill=False, linewidth=2)
#
# fig, ax = plt.subplots()
# ax.set_aspect('equal')
# ax.add_patch(ell_patch)
# ax.scatter(pts_x, pts_y, c=c_array)
#



class Constraint(object):

    def __init__(self):
        pass

