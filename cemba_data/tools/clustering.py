import numpy as np
import pandas as pd
from concurrent.futures import ProcessPoolExecutor, as_completed
from scanpy.utils import get_igraph_from_adjacency
import leidenalg
from natsort import natsorted
from kmodes.kmodes import KModes


def _multi_leiden_clustering(adata, n=100, copy=False, directed=True, seed_of_seeds=0, resolution=1,
                             partition_type=None, partition_kwargs=None, use_weights=True, n_iterations=-1,
                             cpu=1):
    """Modified from scanpy"""
    adata = adata.copy() if copy else adata
    # are we clustering a user-provided graph or the default AnnData one?
    if 'neighbors' not in adata.uns:
        raise ValueError('You need to run `pp.neighbors` first to compute a neighborhood graph.')
    adjacency = adata.uns['neighbors']['connectivities']

    # convert it to igraph
    g = get_igraph_from_adjacency(adjacency, directed=directed)

    # generate n different seeds for each single leiden partition
    np.random.seed(seed_of_seeds)
    random_states = np.random.choice(range(99999), size=n, replace=False)
    step = max(int(n / cpu), 5)
    random_state_chunks = [random_states[i: min(i + step, n)] for i in range(0, n, step)]

    results = []
    with ProcessPoolExecutor(max_workers=cpu) as executor:
        future_dict = {}
        for i, random_state_chunk in enumerate(random_state_chunks):
            # flip to the default partition type if not overriden by the user
            if partition_type is None:
                partition_type = leidenalg.RBConfigurationVertexPartition
            # prepare find_partition arguments as a dictionary, appending to whatever the user provided
            # it needs to be this way as this allows for the accounting of a None resolution
            # (in the case of a partition variant that doesn't take it on input)
            if partition_kwargs is None:
                partition_kwargs = {}
            else:
                if 'seed' in partition_kwargs:
                    print('Warning: seed in the partition_kwargs will be ignored, use seed_of_seeds instead.')
                    del partition_kwargs['seed']
            if use_weights:
                partition_kwargs['weights'] = np.array(g.es['weight']).astype(np.float64)
            partition_kwargs['n_iterations'] = n_iterations
            if resolution is not None:
                partition_kwargs['resolution_parameter'] = resolution
            # clustering proper
            future = executor.submit(_single_leiden,
                                     g=g, random_states=random_state_chunk,
                                     partition_type=partition_type,
                                     partition_kwargs=partition_kwargs)
            future_dict[future] = random_state_chunks

        for future in as_completed(future_dict):
            _ = future_dict[future]
            try:
                data = future.result()
                results.append(data)
            except Exception as exc:
                print(f'_single_leiden generated an exception: {exc}')
                raise exc
    total_result = pd.concat(results, axis=1, sort=True)
    total_result.index = adata.obs_names
    return total_result


def _single_leiden(g, random_states, partition_type, partition_kwargs):
    results = []
    for seed in random_states:
        part = leidenalg.find_partition(g, partition_type, seed=seed, **partition_kwargs)
        groups = np.array(part.membership)
        groups = pd.Categorical(
            values=groups.astype('U'),
            categories=natsorted(np.unique(groups).astype('U')))
        results.append(groups)
    result_df = pd.DataFrame(results, columns=random_states)
    return result_df


def _kmode_on_multi_leiden(multi_leiden_result, k, verbose=0):
    results = multi_leiden_result
    _k = k

    km = KModes(n_clusters=_k, verbose=verbose)
    clusters = km.fit_predict(results)
    final_cluster = pd.Series(clusters, index=results.index)

    nrows, ncols = results.shape
    pureness_records = {}
    completeness_records = {}
    cell_ambiguity_records = {}
    for cluster, sub_df in results.groupby(final_cluster):
        all_modes = sub_df.mode().T.iloc[:, 0]
        all_pureness = []
        all_completeness = []
        sub_rows = sub_df.shape[0]
        for col_name in sub_df.columns:
            # Pureness
            # for each random run, calculate the portion of mode cluster
            # in all cells assigned to this sub_df (cluster)
            mode_count = (sub_df[col_name] == all_modes[col_name]).sum()
            pureness = mode_count / sub_rows
            all_pureness.append(pureness)

            # completeness
            total_mode_count = (results[col_name] == all_modes[col_name]).sum()
            completeness = mode_count / total_mode_count
            all_completeness.append(completeness)

        pureness_records[cluster] = np.array(all_pureness).mean()
        completeness_records[cluster] = np.array(all_completeness).mean()

        for cell, row in sub_df.iterrows():
            portion = (row == all_modes).sum() / ncols
            cell_ambiguity_records[cell] = 1 - portion
    pureness = pd.Series(pureness_records)
    completeness = pd.Series(completeness_records)
    cell_ambiguity = pd.Series(cell_ambiguity_records)

    final_portion = final_cluster.value_counts() / final_cluster.size
    weighted_cluster_pureness = pureness * final_portion
    overall_pureness = weighted_cluster_pureness.sum()
    weighted_cluster_completeness = completeness * final_portion
    overall_completeness = weighted_cluster_completeness.sum()

    result_dict = {'cluster': final_cluster,
                   'n_cluster': final_cluster.unique().size,
                   'cluster_pureness': pureness,
                   'weighted_cluster_pureness': weighted_cluster_pureness,
                   'cluster_completeness': completeness,
                   'weighted_cluster_completeness': weighted_cluster_completeness,
                   'cell_ambiguity': cell_ambiguity,
                   'overall_pureness': overall_pureness,
                   'overall_completeness': overall_completeness,
                   'k': _k}
    return result_dict


def _multi_kmode_clustering(multi_leiden_result, k='auto', cpu=1):
    results = multi_leiden_result
    if k == 'auto':
        # mode of n_cluster of all random runs
        mode_k = int(results.apply(lambda i: i.cat.categories.size, axis=0).mode())
        _k = list(range(max(2, mode_k - 5), mode_k + 15))
    else:
        if not isinstance(k, list):
            raise TypeError(f'k for _multi_kmode_clustering need to be a list of ks, got type {type(k)}')
        _k = k

    result_dict = {}
    with ProcessPoolExecutor(max_workers=cpu) as executor:
        future_dict = {}
        for k in _k:
            future = executor.submit(_kmode_on_multi_leiden,
                                     multi_leiden_result=results,
                                     k=k)
            future_dict[future] = k

        for future in as_completed(future_dict):
            k = future_dict[future]
            try:
                data = future.result()
                result_dict[k] = data
            except Exception as exc:
                print(f'_kmode_on_multi_leiden generated an exception (k={k}): {exc}')
                raise
    return result_dict


def _select_optimal_k(kmode_results, pureness_cutoff=0.99, completeness_cutoff=0.95):
    overall_dict = {'pureness': {},
                    'completeness': {},
                    'n_cluster': {}}
    for k, result_dict in kmode_results.items():
        overall_dict['pureness'][k] = result_dict['overall_pureness']
        overall_dict['completeness'][k] = result_dict['overall_completeness']
        overall_dict['n_cluster'][k] = result_dict['cluster'].unique().size
    overall_df = pd.DataFrame(overall_dict).reset_index().rename(columns={'index': 'k'})
    n_cluster_max = overall_df['n_cluster'].max()

    pass_cutoff = overall_df[(overall_df['pureness'] > pureness_cutoff) &
                             (overall_df['completeness'] > completeness_cutoff)]
    rows = []
    for i, row in pass_cutoff.iterrows():
        rows.append(row)
        if row['n_cluster'] == n_cluster_max:
            # break when see the first max n_cluster, additional row is useless
            break
    pass_cutoff = pd.DataFrame(rows)
    optimal_k = int(pass_cutoff['k'].max())
    return optimal_k


def _filter_cell_and_cluster(result_dict, cell_ambiguity_cutoff=0.01, cluster_portion_cutoff=0.005):
    """
    After determine the resolution and K,
    trimming the final cluster and cells based on ambiguity and cluster size.
    Small size cluster are not able to obtain supervised model.
    """
    cell_data = {
        'LK_cell_ambiguity': result_dict['cell_ambiguity'],
        'LK_cluster': result_dict['cluster']
    }
    cell_data = pd.DataFrame(cell_data)

    cluster_pureness = result_dict['cluster_pureness']
    cluster_completeness = result_dict['cluster_completeness']
    cell_data['LK_cluster_pureness'] = cell_data['cluster'].map(cluster_pureness)
    cell_data['LK_cluster_completeness'] = cell_data['cluster'].map(cluster_completeness)

    # ambiguity judge
    ambiguity_judge = cell_data['LK_cell_ambiguity'] <= cell_ambiguity_cutoff
    # cluster size judge, after ambiguity
    _pass_cell_data = cell_data[ambiguity_judge]
    cluster_size_judge = _pass_cell_data['LK_cluster'].value_counts() > \
                         _pass_cell_data.shape[0] * cluster_portion_cutoff
    pass_clusters = cluster_size_judge[cluster_size_judge].index
    cluster_judge = cell_data['LK_cluster'].apply(lambda i: i in pass_clusters)

    # combine judges
    cell_data['LK_final_judge'] = cluster_judge & ambiguity_judge

    # mask cell's cluster
    cell_data['LK_masked_cluster'] = cell_data.apply(lambda i: str(i['cluster']) if i['final_judge'] else np.nan,
                                                     axis=1)
    return cell_data


# this function deal with real clustering, it save kmode results for different resolution and k
def leiden_kmode_clustering(adata, resolutions, kmode_ks='auto', cpu=1,
                            leiden_repeats=300):
    n_cells = adata.X.shape[0]
    hard_k_min = 5
    hard_k_max = int(n_cells / 50)  # at maximum, ave cluster cell number should >= 50

    total_results = {}
    for _resolution in resolutions:
        print(f'Running {leiden_repeats} clustering with resolution {_resolution}')
        results = _multi_leiden_clustering(adata, cpu=cpu, n=300, resolution=_resolution)
        mode_k = int(results.apply(lambda i: i.cat.categories.size, axis=0).mode())
        if kmode_ks == 'auto':
            # generate a list based on mode k
            _kmode_ks = list(range(max(mode_k - 10, hard_k_min), min(int(mode_k * 2), hard_k_max), 2))
            if len(_kmode_ks) == 0:
                # mode_k is too extreme
                print(f'Resolution {_resolution} have {mode_k} clusters, '
                      f'which seems too extreme. If you really want to run Kmode on this, '
                      f'set kmode_ks manually.')
                total_results[_resolution] = None
                continue
        else:
            _kmode_ks = kmode_ks
        kmin = np.array(_kmode_ks).min()
        kmax = np.array(_kmode_ks).max()
        print(f'Running KMode Clustering on {leiden_repeats} random results. '
              f'{len(_kmode_ks)} different K value (min: {kmin}, max {kmax})')
        kmode_results = _multi_kmode_clustering(results,
                                                k=_kmode_ks,
                                                cpu=cpu)
        total_results[_resolution] = kmode_results
    print("Save all KMode results into adata.uns['leiden_kmode_results']")
    print("Save all parameters into adata.uns['leiden_kmode_parameter']")
    adata.uns['leiden_kmode_results'] = total_results
    adata.uns['leiden_kmode_parameter'] = {
        'resolutions': resolutions,
        'kmode_ks': kmode_ks,
        'hard_k_min': hard_k_min,
        'hard_k_max': hard_k_max,
        'leiden_repeats': leiden_repeats
    }
    return


# this function filter the leiden_kmode_clustering result to automatically
# determine optimal resolution and k, and annotate cells about their cluster and ambiguity info.
def judge_leiden_kmode_results(adata,
                               pureness_cutoff=0.99,
                               completeness_cutoff=0.95,
                               cell_ambiguity_cutoff=0.01,
                               cluster_portion_cutoff=0.005):
    if 'leiden_kmode_results' not in adata.uns:
        raise KeyError('leiden_kmode_results not found in adata.uns, '
                       'make sure you run leiden_kmode_clustering first.')
    total_results = adata.uns['leiden_kmode_results']
    parameters = adata.uns['leiden_kmode_parameter']
    resolutions = parameters['resolutions']

    best_resolution = None
    max_opt_k = -1
    for resolution in resolutions:
        kmode_results = total_results[resolution]
        opt_k = _select_optimal_k(kmode_results,
                                  pureness_cutoff=pureness_cutoff,
                                  completeness_cutoff=completeness_cutoff)
        print(resolution, opt_k)
        if opt_k is not None:
            if opt_k > max_opt_k:
                best_resolution = resolution
                max_opt_k = opt_k

    if best_resolution is not None:
        best_k_result = total_results[best_resolution][max_opt_k]
        cell_data = _filter_cell_and_cluster(best_k_result,
                                             cell_ambiguity_cutoff=cell_ambiguity_cutoff,
                                             cluster_portion_cutoff=cluster_portion_cutoff)
        final_cell_count = cell_data["LK_final_judge"].sum()
        final_cluster_count = cell_data["LK_masked_cluster"].dropna().unique().size
        print(f'Got {final_cell_count} low ambiguity cells '
              f'in {final_cluster_count} high quality clusters')
        for col_name, col in cell_data.iteritems():
            adata.obs[col_name] = col
    else:
        print('None of the resolution and K value combination fulfill both the pureness and completeness cutoff. '
              'Try loosen the cutoff or change the resolution and K ranges '
              'in leiden_kmode_clustering and calculate again.')