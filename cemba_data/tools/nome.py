import anndata
import numpy as np
import scipy.sparse as ss


def call_cell_peak(mc_paths, cov_paths, output_paths,
                   cov_cutoff=3, mc_rate_cutoff=0.76):
    chunk_size = 100000

    h5ad_mc_paths = {i.name.split('.')[-3]: i for i in mc_paths}
    h5ad_cov_paths = {i.name.split('.')[-3]: i for i in cov_paths}
    h5ad_out_paths = {i.name.split('.')[-3]: i for i in output_paths}

    for chunk in h5ad_mc_paths.keys():
        mc_path = h5ad_mc_paths[chunk]
        cov_path = h5ad_cov_paths[chunk]
        out_path = h5ad_out_paths[chunk]
        mc_adata = anndata.read_h5ad(mc_path)
        cov_adata = anndata.read_h5ad(cov_path)

        mc_data = mc_adata.X.tocsc()
        cov_data = cov_adata.X.tocsc()

        n_feature = mc_data.shape[1]
        records = []
        for chunk in range(0, n_feature, chunk_size):
            mc_chunk = mc_data[:, chunk:chunk + chunk_size].todense()
            cov_chunk = cov_data[:, chunk:chunk + chunk_size].todense()
            judge = ss.csc_matrix(np.all([(cov_chunk >= cov_cutoff),
                                          (mc_chunk / cov_chunk > mc_rate_cutoff)],
                                         axis=0))
            records.append(judge)
        total_judge = ss.hstack(records)

        adata = anndata.AnnData(X=total_judge,
                                obs=mc_adata.obs,
                                var=mc_adata.var,
                                uns=mc_adata.uns)
        adata.uns['count_type'] = 'peak'
        adata.write(out_path, compression='gzip')
    return