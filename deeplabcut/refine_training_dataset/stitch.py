import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd
import pickle
import re
import scipy.linalg.interpolative as sli
from collections import defaultdict
from itertools import combinations
from math import factorial
from networkx.algorithms.flow import preflow_push
from scipy.linalg import hankel
from scipy.spatial.distance import directed_hausdorff
from statsmodels.tsa.api import SimpleExpSmoothing
from tqdm import tqdm, trange


class Tracklet:
    def __init__(self, data, inds):
        self.data = data
        self.inds = np.array(inds)
        monotonically_increasing = all(a < b for a, b in zip(inds, inds[1:]))
        if not monotonically_increasing:
            idx = np.argsort(inds, kind='mergesort')  # For stable sort with duplicates
            self.inds = self.inds[idx]
            self.data = self.data[idx]
        self._centroid = None

    def __len__(self):
        return self.inds.size

    def __add__(self, other):
        """Join this tracklet to another one."""
        data = np.concatenate((self.data, other.data))
        inds = np.concatenate((self.inds, other.inds))
        return Tracklet(data, inds)

    def __radd__(self, other):
        if other == 0:
            return self
        return self.__add__(other)

    def __lt__(self, other):
        """Test whether this tracklet precedes the other one."""
        return self.end < other.start

    def __gt__(self, other):
        """Test whether this tracklet follows the other one."""
        return self.start > other.end

    def __contains__(self, other_tracklet):
        """Test whether tracklets temporally overlap."""
        return np.isin(self.inds, other_tracklet.inds, assume_unique=True).any()

    def __repr__(self):
        return f'Tracklet of length {len(self)}, ' \
               f'from {self.start} to {self.end}'

    @property
    def xy(self):
        return self.data[..., :2]

    @property
    def centroid(self):
        if self._centroid is None:
            centroid = np.nanmean(self.xy, axis=1)
            if len(centroid) <= 10:
                self._centroid = centroid
            else:
                fit_x = (SimpleExpSmoothing(centroid[:, 0])
                         .fit(smoothing_level=0.5, optimized=False)
                         .fittedvalues)
                fit_y = (SimpleExpSmoothing(centroid[:, 1])
                         .fit(smoothing_level=0.5, optimized=False)
                         .fittedvalues)
                self._centroid = np.c_[fit_x, fit_y]
        return self._centroid

    @property
    def likelihood(self):
        return self.data[..., 2]

    @property
    def start(self):
        return self.inds[0]

    @property
    def end(self):
        return self.inds[-1]

    def contains_duplicates(self, return_indices=False):
        has_duplicates = len(set(self.inds)) != len(self.inds)
        if not return_indices:
            return has_duplicates
        return has_duplicates, np.flatnonzero(np.diff(self.inds) == 0)

    def calc_velocity(self, where='head', norm=True):
        if where == 'tail':
            vel = (np.diff(self.centroid[:3], axis=0)
                   / np.diff(self.inds[:3])[:, np.newaxis])
        elif where == 'head':
            vel = (np.diff(self.centroid[-3:], axis=0)
                   / np.diff(self.inds[-3:])[:, np.newaxis])
        else:
            raise ValueError(f'Unknown where={where}')
        if norm:
            return np.sqrt(np.sum(vel ** 2, axis=1)).mean()
        return vel.mean(axis=0)

    def calc_rate_of_turn(self, where='head'):
        if where == 'tail':
            v = np.diff(self.centroid[:3], axis=0)
        else:
            v = np.diff(self.centroid[-3:], axis=0)
        theta = np.arctan2(v[:, 1], v[:, 0])
        return (theta[1] - theta[0]) / (self.inds[1] - self.inds[0])

    @property
    def is_continuous(self):
        return self.end - self.start + 1 == len(self)

    def immediately_follows(self, other_tracklet, max_gap=1):
        return 0 < self.start - other_tracklet.end <= max_gap

    def distance_to(self, other_tracklet):
        if self in other_tracklet:
            dist = (self.centroid[np.isin(self.inds, other_tracklet.inds)]
                    - other_tracklet.centroid[np.isin(other_tracklet.inds, self.inds)])
            return np.sqrt(np.sum(dist ** 2, axis=1)).mean()
        elif self < other_tracklet:
            return np.sqrt(np.sum((self.centroid[-1] - other_tracklet.centroid[0]) ** 2))
        else:
            return np.sqrt(np.sum((self.centroid[0] - other_tracklet.centroid[-1]) ** 2))

    def motion_affinity_with(self, other_tracklet):
        time_gap = self.time_gap_to(other_tracklet)
        if time_gap > 0:
            if self < other_tracklet:
                d1 = self.centroid[-1] + time_gap * self.calc_velocity(norm=False)
                d2 = other_tracklet.centroid[0] - time_gap * other_tracklet.calc_velocity('tail', False)
                delta1 = other_tracklet.centroid[0] - d1
                delta2 = self.centroid[-1] - d2
            else:
                d1 = other_tracklet.centroid[-1] + time_gap * other_tracklet.calc_velocity(norm=False)
                d2 = self.centroid[0] - time_gap * self.calc_velocity('tail', False)
                delta1 = self.centroid[0] - d1
                delta2 = other_tracklet.centroid[-1] - d2
            return (np.sqrt(np.sum(delta1 ** 2)) + np.sqrt(np.sum(delta2 ** 2))) / 2
        return 0

    def time_gap_to(self, other_tracklet):
        if self in other_tracklet:
            t = 0
        elif self < other_tracklet:
            t = other_tracklet.start - self.end
        else:
            t = self.start - other_tracklet.end
        return t

    def shape_dissimilarity_with(self, other_tracklet):
        if self in other_tracklet:
            dist = np.inf
        elif self < other_tracklet:
            dist = self.undirected_hausdorff(self.xy[-1], other_tracklet.xy[0])
        else:
            dist = self.undirected_hausdorff(self.xy[0], other_tracklet.xy[-1])
        return dist

    def box_overlap_with(self, other_tracklet):
        if self in other_tracklet:
            overlap = 0
        else:
            if self < other_tracklet:
                bbox1 = self.calc_bbox(-1)
                bbox2 = other_tracklet.calc_bbox(0)
            else:
                bbox1 = self.calc_bbox(0)
                bbox2 = other_tracklet.calc_bbox(-1)
            overlap = self.iou(bbox1, bbox2)
        return overlap

    @staticmethod
    def undirected_hausdorff(u, v):
        return max(directed_hausdorff(u, v)[0],
                   directed_hausdorff(v, u)[0])

    @staticmethod
    def iou(bbox1, bbox2):
        x1 = max(bbox1[0], bbox2[0])
        y1 = max(bbox1[1], bbox2[1])
        x2 = min(bbox1[2], bbox2[2])
        y2 = min(bbox1[3], bbox2[3])
        w = max(0, x2 - x1)
        h = max(0, y2 - y1)
        wh = w * h
        return wh / ((bbox1[2] - bbox1[0]) * (bbox1[3] - bbox1[1])
                     + (bbox2[2] - bbox2[0]) * (bbox2[3] - bbox2[1])
                     - wh)

    def calc_bbox(self, ind):
        xy = self.xy[ind]
        bbox = np.empty(4)
        bbox[:2] = np.nanmin(xy, axis=0)
        bbox[2:] = np.nanmax(xy, axis=0)
        return bbox

    @staticmethod
    def hankelize(xy):
        ncols = int(np.ceil(len(xy) * 2 / 3))
        nrows = len(xy) - ncols + 1
        mat = np.empty((2 * nrows, ncols))
        mat[::2] = hankel(xy[:nrows, 0], xy[-ncols:, 0])
        mat[1::2] = hankel(xy[:nrows, 1], xy[-ncols:, 1])
        return mat

    def to_hankelet(self):
        # See Li et al., 2012. Cross-view Activity Recognition using Hankelets.
        # As proposed in the paper, the Hankel matrix can either be formed from
        # the tracklet's centroid or its normalized velocity.
        # vel = np.diff(self.centroid, axis=0)
        # vel /= np.linalg.norm(vel, axis=1, keepdims=True)
        # return self.hankelize(vel)
        return self.hankelize(self.centroid)

    def dynamic_dissimilarity_with(self, other_tracklet):
        """
        Compute a dissimilarity score between Hankelets.
        This metric efficiently captures the degree of alignment of
        the subspaces spanned by the columns of both matrices.

        See Li et al., 2012.
            Cross-view Activity Recognition using Hankelets.
        """
        hk1 = self.to_hankelet()
        hk1 /= np.linalg.norm(hk1)
        hk2 = other_tracklet.to_hankelet()
        hk2 /= np.linalg.norm(hk2)
        min_shape = min(hk1.shape + hk2.shape)
        temp1 = (hk1 @ hk1.T)[:min_shape, :min_shape]
        temp2 = (hk2 @ hk2.T)[:min_shape, :min_shape]
        return 2 - np.linalg.norm(temp1 + temp2)

    def dynamic_similarity_with(self, other_tracklet, tol=0.01):
        """
        Evaluate the complexity of the tracklets' underlying dynamics
        from the rank of their Hankel matrices, and assess whether
        they originate from the same track. The idea is that if two
        tracklets are part of the same track, they can be approximated
        by a low order regressor. Conversely, tracklets belonging to
        different tracks will require a higher order regressor.

        See Dicle et al., 2013.
            The Way They Move: Tracking Multiple Targets with Similar Appearance.
        """
        # TODO Add missing data imputation
        joint_tracklet = self + other_tracklet
        joint_rank = joint_tracklet.estimate_rank(tol)
        rank1 = self.estimate_rank(tol)
        rank2 = other_tracklet.estimate_rank(tol)
        return (rank1 + rank2) / joint_rank - 1

    def estimate_rank(self, tol):
        """
        Estimate the (low) rank of a noisy matrix via
        hard thresholding of singular values.

        See Gavish & Donoho, 2013.
            The optimal hard threshold for singular values is 4/sqrt(3)
        """
        mat = self.to_hankelet()
        # nrows, ncols = mat.shape
        # beta = nrows / ncols
        # omega = 0.56 * beta ** 3 - 0.95 * beta ** 2 + 1.82 * beta + 1.43
        _, s, _ = sli.svd(mat, min(10, min(mat.shape)))
        # return np.argmin(s > omega * np.median(s))
        eigen = s ** 2
        diff = np.abs(np.diff(eigen / eigen[0]))
        return np.argmin(diff > tol)

    def plot(self, centroid_only=True, color='r'):
        plt.plot(self.inds, self.centroid, c=color, lw=2)
        if not centroid_only:
            plt.plot(self.inds, self.xy[..., 0], c=color, lw=1)
            plt.plot(self.inds, self.xy[..., 1], c=color, lw=1)


class TrackletStitcher:
    def __init__(self, pickle_file, n_tracks, min_length=5, split_tracklets=True):
        if n_tracks < 2:
            raise ValueError('There must at least be two tracks to reconstruct.')

        if min_length < 3:
            raise ValueError('A tracklet must have a minimal length of 3.')

        self.filename = pickle_file
        self.n_tracks = n_tracks
        self.G = None
        self.paths = None
        self.tracks = None

        with open(pickle_file, 'rb') as file:
            tracklets = pickle.load(file)
        self.header = tracklets.pop("header")
        if not len(tracklets):
            raise IOError("Tracklets are empty.")

        self.tracklets = []
        self.residuals = []
        last_frames = []
        for dict_ in tracklets.values():
            inds, data = zip(*[(self.get_frame_ind(k), v) for k, v in dict_.items()])
            last_frames.append(inds[-1])
            inds = np.asarray(inds)
            data = np.asarray(data)
            # Input data as (nframes, nbodyparts, 3)
            nrows, ncols = data.shape
            temp = data.reshape((nrows, ncols // 3, 3))
            all_nans = np.isnan(temp).all(axis=(1, 2))
            if all_nans.any():
                temp = temp[~all_nans]
                inds = inds[~all_nans]
            if not inds.size:
                continue
            tracklet = Tracklet(temp, inds)
            if not tracklet.is_continuous and split_tracklets:
                tracklet = self.split_tracklet(tracklet)
            if not isinstance(tracklet, list):
                tracklet = [tracklet]
            for t in tracklet:
                if len(t) >= min_length:
                    self.tracklets.append(t)
                elif len(t) < min_length:
                    self.residuals.append(t)
        self.n_frames = max(last_frames) + 1

        # Note that if tracklets are very short, some may actually be part of the same track
        # and thus incorrectly reflect separate track endpoints...
        self._first_tracklets = sorted(self, key=lambda t: t.start)[:self.n_tracks]
        self._last_tracklets = sorted(self, key=lambda t: t.end)[-self.n_tracks:]

    def __getitem__(self, item):
        return self.tracklets[item]

    def __len__(self):
        return len(self.tracklets)

    @staticmethod
    def get_frame_ind(s):
        return int(re.findall(r"\d+", s)[0])

    @staticmethod
    def split_tracklet(tracklet):
        idx = np.flatnonzero(np.diff(tracklet.inds) != 1) + 1
        inds_new = np.split(tracklet.inds, idx)
        data_new = np.split(tracklet.data, idx)
        return [Tracklet(data, inds) for data, inds in zip(data_new, inds_new)]

    def compute_max_gap(self):
        gap = defaultdict(list)
        for tracklet1, tracklet2 in combinations(self, 2):
            gap[tracklet1].append(tracklet1.time_gap_to(tracklet2))
        max_gap = 0
        for vals in gap.values():
            for val in sorted(vals):
                if val > 0:
                    if val > max_gap:
                        max_gap = val
                    break
        return max_gap

    def build_graph(self, max_gap=None):
        if not max_gap:
            max_gap = int(1.5 * self.compute_max_gap())

        self._mapping = {tracklet: {'in': f'{i}in', 'out': f'{i}out'}
                         for i, tracklet in enumerate(self)}
        self._mapping_inv = {label: k for k, v in self._mapping.items()
                             for label in v.values()}

        self.G = nx.DiGraph()
        self.G.add_node('source', demand=-self.n_tracks)
        self.G.add_node('sink', demand=self.n_tracks)
        nodes_in, nodes_out = zip(*[v.values() for v in self._mapping.values()])
        self.G.add_nodes_from(nodes_in, demand=1)
        self.G.add_nodes_from(nodes_out, demand=-1)
        self.G.add_edges_from(zip(nodes_in, nodes_out), capacity=1)
        self.G.add_edges_from(zip(['source'] * len(self), nodes_in), capacity=1)
        self.G.add_edges_from(zip(nodes_out, ['sink'] * len(self)), capacity=1)
        n_combinations = int(factorial(len(self)) / (2 * factorial(len(self) - 2)))
        for tracklet1, tracklet2 in tqdm(combinations(self, 2), total=n_combinations):
            time_gap = tracklet1.time_gap_to(tracklet2)
            if 0 < time_gap <= max_gap:
                # The algorithm works better with integer weights
                w = int(100 * self.calculate_weight(tracklet1, tracklet2))
                if tracklet2 > tracklet1:
                    self.G.add_edge(self._mapping[tracklet1]['out'],
                                    self._mapping[tracklet2]['in'],
                                    weight=w, capacity=1)
                else:
                    self.G.add_edge(self._mapping[tracklet2]['out'],
                                    self._mapping[tracklet1]['in'],
                                    weight=w, capacity=1)

    def stitch(self):
        if self.G is None:
            raise ValueError('Inexistent graph. Call `build_graph` first')

        try:
            _, self.flow = nx.capacity_scaling(self.G)
            self.paths = self.reconstruct_paths()
        except nx.exception.NetworkXUnfeasible:
            print('No optimal solution found. Employing black magic...')
            # Let us prune the graph by removing all source and sink edges
            # but those connecting the `n_tracks` first and last tracklets.
            in_to_keep = [self._mapping[first_tracklet]['in']
                          for first_tracklet in self._first_tracklets]
            out_to_keep = [self._mapping[last_tracklet]['out']
                           for last_tracklet in self._last_tracklets]
            in_to_remove = (set(node for _, node in self.G.out_edges('source'))
                            .difference(in_to_keep))
            out_to_remove = (set(node for node, _ in self.G.in_edges('sink'))
                             .difference(out_to_keep))
            self.G.remove_edges_from(zip(['source'] * len(in_to_remove), in_to_remove))
            self.G.remove_edges_from(zip(out_to_remove, ['sink'] * len(out_to_remove)))
            # Preflow push seems to work slightly better than shortest
            # augmentation path..., and is more computationally efficient.
            paths = []
            for path in nx.node_disjoint_paths(self.G, 'source', 'sink',
                                               preflow_push, self.n_tracks):
                temp = set()
                for node in path[1:-1]:
                    self.G.remove_node(node)
                    temp.add(self._mapping_inv[node])
                paths.append(list(temp))
            incomplete_tracks = self.n_tracks - len(paths)
            if incomplete_tracks == 1:  # All remaining nodes ought to belong to the same track
                nodes = set(self._mapping_inv[node] for node in self.G
                            if node not in ('source', 'sink'))
                # Verify whether there are overlapping tracklets
                for t1, t2 in combinations(nodes, 2):
                    if t1 in t2:
                        # Pick the segment that minimizes "smoothness", computed here
                        # with the coefficient of variation of the differences.
                        nodes.remove(t1)
                        nodes.remove(t2)
                        track = sum(nodes)
                        hyp1 = track + t1
                        hyp2 = track + t2
                        dx1 = np.diff(hyp1.centroid, axis=0)
                        cv1 = dx1.std() / np.abs(dx1).mean()
                        dx2 = np.diff(hyp2.centroid, axis=0)
                        cv2 = dx2.std() / np.abs(dx2).mean()
                        if cv1 < cv2:
                            nodes.add(t1)
                            self.residuals.append(t2)
                        else:
                            nodes.add(t2)
                            self.residuals.append(t1)
                paths.append(list(nodes))
            elif incomplete_tracks > 1:
                raise NotImplementedError
            self.paths = paths
        finally:
            self._finalize_tracks()

    def _finalize_tracks(self):
        tracks = np.asarray([sum(path) for path in self.paths])
        # Greedily incorporate the residual tracklets
        residuals = sorted(self.residuals, key=len)
        for _ in trange(len(residuals)):
            residual = residuals.pop()
            easy_fit = [residual not in track for track in tracks]
            inds = np.flatnonzero(easy_fit)
            if inds.size == 1:
                tracks[inds[0]] += residual
            elif inds.size >= 2:
                # Disambiguate a residual from its distance to the candidate tracklets
                dist = [residual.distance_to(track) for track in tracks[inds]]
                ind = inds[np.argmin(dist)]
                tracks[ind] += residual
        self.tracks = tracks

    def write_tracks(self, output_name=''):
        if self.tracks is None:
            raise ValueError('No tracks were found. Call `stitch` first')

        scorer = self.header.get_level_values('scorer').unique().to_list()
        individuals = [f'ind{i}' for i in range(self.n_tracks)]
        bpts = self.header.get_level_values('bodyparts').unique().to_list()
        coords = ['x', 'y', 'likelihood']
        ncols = len(bpts) * len(coords)
        index = range(self.n_frames)
        columns = pd.MultiIndex.from_product([scorer, individuals, bpts, coords],
                                             names=['scorer', 'individuals', 'bodyparts', 'coords'])
        data = np.full((self.n_frames, len(columns)), np.nan)
        for n, track in enumerate(self.tracks):
            data[track.inds, n * ncols:(n + 1) * ncols] = track.data.reshape((len(track), ncols))
        df = pd.DataFrame(data, index=index, columns=columns)
        if not output_name:
            output_name = self.filename.replace('pickle', 'h5')
        df.to_hdf(output_name, 'df_with_missing', format='table', mode='w')

    @staticmethod
    def calculate_weight(tracklet1, tracklet2):
        return (
                -np.log(tracklet1.box_overlap_with(tracklet2) + np.finfo(float).eps)
                + tracklet1.distance_to(tracklet2)
                + tracklet1.shape_dissimilarity_with(tracklet2)
                + tracklet1.dynamic_dissimilarity_with(tracklet2)
        )

    @property
    def weights(self):
        if self.G is None:
            raise ValueError('Inexistent graph. Call `build_graph` first')

        return nx.get_edge_attributes(self.G, 'weight')

    def draw_graph(self, with_weights=False):
        if self.G is None:
            raise ValueError('Inexistent graph. Call `build_graph` first')

        pos = nx.spring_layout(self.G)
        nx.draw_networkx(self.G, pos)
        if with_weights:
            nx.draw_networkx_edge_labels(self.G, pos, edge_labels=self.weights)

    def plot_paths(self, colormap='Set2'):
        if self.paths is None:
            raise ValueError('No paths were found. Call `stitch` first')

        for path in self.paths:
            length = len(path)
            colors = plt.get_cmap(colormap, length)(range(length))
            for tracklet, color in zip(path, colors):
                tracklet.plot(color=color)

    def plot_tracks(self, colormap='viridis'):
        if self.tracks is None:
            raise ValueError('No tracks were found. Call `stitch` first')

        colors = plt.get_cmap(colormap, self.n_tracks)(range(self.n_tracks))
        for track, color in zip(self.tracks, colors):
            track.plot(color=color)

    def reconstruct_paths(self, edges=None):
        paths = []
        if edges is None:
            edges = []
            init = [self._mapping_inv[a] for a, b in self.flow['source'].items() if b == 1]
            for k, v in self.flow.items():
                if all(s not in k for s in ['source', 'sink', 'in']):
                    for i, j in v.items():
                        if j == 1:
                            node = self._mapping_inv[k]
                            if i != 'sink':
                                edges.append((node, self._mapping_inv[i]))
                                break
                            elif node in init:
                                # That node (i.e., tracklet) demands and supplies flow
                                # directly from the source to the sink; it thus constitutes
                                # an entire track on its own.
                                paths.append([node])
                                break
        G = nx.Graph(edges)
        paths.extend([sorted(tracklets, key=lambda t: t.start)
                      for tracklets in nx.connected_components(G)])
        return paths


def stitch_tracklets(
    pickle_file,
    n_tracks,
    min_length=5,
    split_tracklets=True,
    output_name='',
):
    """
    Stitch sparse tracklets into full tracks via a graph-based,
    minimum-cost flow optimization problem.

    Parameters
    ----------
    pickle_file : str
        Path to the pickle file containing the tracklets.
        It is obtained after deeplabcut.convert_detections2tracklets()
        and typically ends with _bx or _sk.pickle.

    n_tracks : int
        Number of tracks to reconstruct.
        This is equivalent to the number of animals in the scene.

    min_length : int, optional
        Tracklets less than `min_length` frames of length
        are considered to be residuals; i.e., they do not participate
        in building the graph and finding the solution to the
        optimization problem, but are rather added last after
        "almost-complete" tracks are formed. The higher the value,
        the lesser the computational cost, but the higher the chance of
        discarding relatively long and reliable tracklets that are
        essential to solving the stitching task.
        Default is 5, and must be 3 at least.

    split_tracklets : bool, optional
        By default, tracklets whose time indices are not consecutive integers
        are split in shorter tracklets whose time continuity is guaranteed.
        This is for example very powerful to get rid of tracking errors
        (e.g., identity switches) which are often signaled by a missing
        time frame at the moment they occur. Note though that for long
        occlusions where tracker re-identification capability can be trusted,
        setting `split_tracklets` to False is preferable.

    output_name : str, optional
        Name of the output h5 file.
        By default, tracks are automatically stored into the same directory
        as the pickle file and with its name.

    Returns
    -------
    A TrackletStitcher object
    """
    stitcher = TrackletStitcher(pickle_file, n_tracks, min_length, split_tracklets)
    stitcher.build_graph()
    stitcher.stitch()
    stitcher.write_tracks(output_name)
    return stitcher
