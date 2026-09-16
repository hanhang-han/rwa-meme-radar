// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title PairRegistry — on-chain registry of verified meme ↔ stock token pairs.
/// @notice RWA Meme Radar (cliperx.com/dashboard) registers pairs here once
///         on-chain evidence (shared DEX pool between a meme token and an
///         xStocks token) has been verified off-chain. Anyone can read the
///         registry; only the radar service wallet can write.
contract PairRegistry {
    struct Pair {
        address meme;     // meme token contract on X Layer
        address stock;    // xStocks token contract on X Layer
        string  ticker;   // stock ticker, e.g. "NVDA"
        bytes32 evidence; // keccak256 of the verification evidence JSON
        uint64  registeredAt;
    }

    address public owner;
    Pair[] private _pairs;
    mapping(bytes32 => uint256) private _indexOf; // key(meme, stock) => index + 1

    event Registered(address indexed meme, address indexed stock, string ticker, bytes32 evidence, uint256 index);
    event OwnershipTransferred(address indexed from, address indexed to);

    error NotOwner();
    error ZeroAddress();
    error AlreadyRegistered();

    constructor() {
        owner = msg.sender;
    }

    function _key(address meme, address stock) internal pure returns (bytes32) {
        return keccak256(abi.encodePacked(meme, stock));
    }

    /// @notice Register a verified pair. Idempotent per (meme, stock).
    function register(address meme, address stock, string calldata ticker, bytes32 evidence) external {
        if (msg.sender != owner) revert NotOwner();
        if (meme == address(0) || stock == address(0)) revert ZeroAddress();
        bytes32 key = _key(meme, stock);
        if (_indexOf[key] != 0) revert AlreadyRegistered();
        _pairs.push(Pair(meme, stock, ticker, evidence, uint64(block.timestamp)));
        _indexOf[key] = _pairs.length;
        emit Registered(meme, stock, ticker, evidence, _pairs.length - 1);
    }

    function count() external view returns (uint256) {
        return _pairs.length;
    }

    function get(uint256 index) external view returns (Pair memory) {
        return _pairs[index];
    }

    function all() external view returns (Pair[] memory) {
        return _pairs;
    }

    function registered(address meme, address stock) external view returns (bool) {
        return _indexOf[_key(meme, stock)] != 0;
    }

    function transferOwnership(address next) external {
        if (msg.sender != owner) revert NotOwner();
        emit OwnershipTransferred(owner, next);
        owner = next;
    }
}
