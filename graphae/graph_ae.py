import torch
from torch.nn import Linear, LayerNorm, Dropout
#from graphae import encoder, decoder
from graphae.graph_transformer import Transformer, PositionalEncoding
from graphae.graph_transfromer2 import GraphTransformer
from graphae import permuter
from graphae.data import DenseGraphBatch
from graphae.side_tasks import PropertyPredictor


class GraphEncoder(torch.nn.Module):
    def __init__(self, hparams):
        super().__init__()
        self.posiotional_embedding = PositionalEncoding(32)
        self.graph_transformer = self.encoder = Transformer(
            hidden_dim=256,
            k_dim=64,
            v_dim=64,
            num_heads=12,
            ppf_hidden_dim=1024,
            num_layers=12 #12
        )
        # 11 edge features (including empty edge) and 26 node features + emb node feature and emb node edge
        self.fc_in = Linear(2 * (26 + 1) + 11 + 1, 256)
        self.layer_norm = LayerNorm(256)
        self.dropout = Dropout(0.1)

    def add_emb_node_and_feature(self, node_features, edge_features, mask):
        batch_size, num_nodes = node_features.size(0), node_features.size(1)
        num_node_features, num_edge_features = node_features.size(-1), edge_features.size(-1)
        node_features = torch.cat((node_features, node_features.new_zeros(batch_size, num_nodes, 1)), dim=2)
        emb_node = torch.Tensor(num_node_features * [0] + [1]).view(1, 1, num_node_features + 1).type_as(node_features)
        emb_node = emb_node.expand(batch_size, -1, -1)
        node_features = torch.cat((emb_node, node_features), dim=1)
        emb_node_edges = torch.Tensor(
            num_edge_features * [0] + [1]).view(1, 1, 1, num_edge_features + 1).type_as(edge_features)
        emb_node_edges_row = emb_node_edges.expand(batch_size, num_nodes, -1, -1)
        emb_node_edges_col = emb_node_edges.expand(batch_size, -1, num_nodes + 1, -1)
        edge_features = torch.cat((edge_features, edge_features.new_zeros(batch_size, num_nodes, num_nodes, 1)), dim=3)
        edge_features = torch.cat((emb_node_edges_row, edge_features), dim=2)
        edge_features = torch.cat((emb_node_edges_col, edge_features), dim=1)
        mask = torch.cat((torch.Tensor(batch_size * [1]).view(batch_size, 1).type_as(mask), mask), dim=1)
        return node_features, edge_features, mask

    def forward(self, node_features, edge_features, mask):
        node_features, edge_features, mask = self.add_emb_node_and_feature(node_features, edge_features, mask)
        batch_size, num_nodes = node_features.size(0), node_features.size(1)

        edge_mask = mask.unsqueeze(1) * mask.unsqueeze(2)
        node_features_combined = torch.cat(
            (node_features.unsqueeze(2).repeat(1, 1, num_nodes, 1),
             node_features.unsqueeze(1).repeat_interleave(num_nodes, dim=1)),
            dim=-1)
        x = torch.cat((edge_features, node_features_combined), dim=-1)
        x = self.layer_norm(self.dropout(self.fc_in(x)))
        x = self.graph_transformer(x, mask=edge_mask)
        node_features = torch.diagonal(x, dim1=1, dim2=2).transpose(1, 2)
        graph_emb, node_features = node_features[:, 0], node_features[:, 1:]
        return graph_emb, node_features


class GraphDecoder(torch.nn.Module):
    def __init__(self, hparams):
        super().__init__()
        self.posiotional_embedding = PositionalEncoding(64)
        self.graph_transformer = self.encoder = Transformer(
            hidden_dim=256,
            k_dim=64,
            v_dim=64,
            num_heads=12,
            ppf_hidden_dim=1024,
            num_layers=12
        )
        self.fc_in = Linear(256 + 2 * 64, 256)
        self.node_fc_out = Linear(256, 20)
        self.edge_fc_out = Linear(256, 5)
        self.dropout = Dropout(0.1)
        self.layer_norm = LayerNorm(256)

    def forward(self, graph_emb, perm, mask):
        batch_size, num_nodes = mask.size(0), mask.size(1)
        edge_mask = mask.unsqueeze(1) * mask.unsqueeze(2)

        pos_emb = self.posiotional_embedding(batch_size, num_nodes)
        if perm is not None:
            pos_emb = torch.matmul(perm, pos_emb)
        pos_emb_combined = torch.cat(
            (pos_emb.unsqueeze(2).repeat(1, 1, num_nodes, 1),
             pos_emb.unsqueeze(1).repeat_interleave(num_nodes, dim=1)),
            dim=-1)

        x = graph_emb.unsqueeze(1).unsqueeze(1).expand(-1, num_nodes, num_nodes, -1)
        x = torch.cat((x, pos_emb_combined), dim=-1)
        x = self.layer_norm(self.dropout(self.fc_in(x)))
        x = self.graph_transformer(x, mask=edge_mask)

        node_features = torch.diagonal(x, dim1=1, dim2=2).transpose(1, 2)
        edge_features = x

        node_features = self.node_fc_out(node_features)
        edge_features = self.edge_fc_out(edge_features)
        self_edge_mask = torch.eye(num_nodes, num_nodes, device=node_features.device).bool().unsqueeze(-1)
        edge_features.masked_fill_(self_edge_mask, 0)
        edge_features = (edge_features + edge_features.permute(0, 2, 1, 3)) / 2

        return node_features, edge_features


class Permuter(torch.nn.Module):
    def __init__(self, hparams):
        super().__init__()
        self.permuter = permuter.Permuter(
            input_dim=256,
        )

    def forward(self, node_features, mask, hard=False, tau=1.0):
        batch_size, num_nodes = mask.size(0), mask.size(1)
        node_features = node_features + torch.randn_like(node_features) * 0.05
        perm = self.permuter(node_features, mask, hard=hard, tau=tau)
        eye = torch.eye(num_nodes, num_nodes).unsqueeze(0).expand(batch_size, -1, -1).type_as(perm)
        mask = mask.unsqueeze(-1).expand(-1, -1, num_nodes)
        perm = torch.where(mask, perm, eye)
        return perm


# TODO: get attn mask for encoder and decoder together
class GraphAE(torch.nn.Module):
    def __init__(self, hparams):
        super().__init__()
        self.encoder = GraphEncoder(hparams)
        self.property_predictor = PropertyPredictor(256, 1024, hparams["num_properties"])
        self.permuter = Permuter(hparams)
        self.decoder = GraphDecoder(hparams)
        self.node_dim = hparams["node_dim"]
        self.num_nodes = hparams["max_num_nodes"]

    def encode(self, graph):
        node_features = graph.node_features
        edge_features = graph.edge_features
        mask = graph.mask

        graph_emb, node_features = self.encoder(
            node_features=node_features,
            edge_features=edge_features,
            mask=mask,
        )
        return graph_emb, node_features

    def forward(self, graph, training, tau):
        mask = graph.mask
        graph_emb, node_features = self.encode(graph=graph)
        properties = self.property_predictor(graph_emb)
        perm = self.permuter(node_features, mask=mask, hard=not training, tau=tau)
        node_logits, edge_logits = self.decoder(
            graph_emb=graph_emb,
            perm=perm,
            mask=mask
        )
        graph_pred = DenseGraphBatch(
            node_features=node_logits,
            edge_features=edge_logits,
            mask=mask,
            molecular_properties=properties
        )
        return graph_pred, graph_emb, perm

    @staticmethod
    def logits_to_one_hot(graph):
        nodes = graph.node_features
        edges = graph.edge_features
        batch_size, num_nodes = nodes.size(0), nodes.size(1)
        element_type = torch.argmax(nodes[:, :, :11], axis=-1).unsqueeze(-1)
        element_type = torch.zeros((batch_size, num_nodes, 11)).type_as(element_type).scatter_(2, element_type, 1)
        charge_type = torch.argmax(nodes[:, :, 11:16], axis=-1).unsqueeze(-1)
        charge_type = torch.zeros((batch_size, num_nodes, 5)).type_as(charge_type).scatter_(2, charge_type, 1)
        num_explicit_hydrogens = torch.argmax(nodes[:, :, 16:], axis=-1).unsqueeze(-1)
        num_explicit_hydrogens = torch.zeros((batch_size, num_nodes, 4)).type_as(num_explicit_hydrogens).scatter_(
            2, num_explicit_hydrogens, 1)
        nodes = torch.cat((element_type, charge_type, num_explicit_hydrogens), dim=-1)
        edges_shape = edges.shape
        edges = torch.argmax(edges, axis=-1).unsqueeze(-1)
        edges = torch.zeros(edges_shape).type_as(edges).scatter_(3, edges, 1)
        graph.node_features = nodes
        graph.edge_features = edges
        return graph
